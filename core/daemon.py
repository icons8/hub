"""hubd — the single core process, the only writer of the journal.

Loop: facts from the inbox and from session output, by time (collect) -> look at the sessions (Claude Code registry) -> timers that fired.
Each fact: into the journal -> fold -> actions for the host -> the result comes back as a fact.
On start the state is restored from the journal, then reconciled with tmux: a session that is live per the journal but not running is spawned.
"""
import datetime
import glob
import json
import os
import re
import itertools
import shutil
import stat
import sys
import threading
import time

import uuid

import platforms
from core import admit, appwatch, askexpired, board, budget, busd, busstate, catalog, codexslot, configwatch, coretext, corewrite, cutout, delegations, executors, forms, i18n, journal, limitresume, lock, mcplogin, \
    mcpwatch, minihub, modrun, modstate, modules, news, oldlayout, owner, parking, profcap, profiles, providers, redact, reducer, restart, specmode, spool, \
    statemark, system, taskrec, workspace, zones
from core import machines, ownclaude, runjobs
from core.machines import LIVE
# getattr: a process of v1.4.0 (the stats report, the watchdog) may import this file during the update's self-check with
# its own core.machines in memory, without SLEEP (tests/test_old_page_server)
SLEEP = getattr(machines, "SLEEP", ("asleep", "parked", "retired"))
from hosts import worktree
import subprocess


# the core's reasons a session reads in the journal's facts (a task that did not start, a kept copy, the gateway); the owner
# sees them in his language where the marks reach him (core/coretext.py)
GATEWAY_OFF = coretext.T("core.daemon.gateway_off", "switched off in the settings")
GATEWAY_STREAM = coretext.T("core.daemon.gateway_stream", "only with the stream-json host")
NO_LOGIN_MODULE = coretext.T("core.daemon.no_login_module", "no sign-in module — sessions come up directly")
CATALOG_EVERY = 30  # seconds between reconciling the catalog with the sets on disk
SLEPT_MIN = 60   # s; the wall clock ran ahead of the awake clock by this much since the last pass: the machine slept (check_slept)
PROFILE_RETRY = 600   # s; a waiting eviction of an agent's CLAUDE.md that failed (a read-only folder) is retried this rarely
PIN_FILE_MAX = 2 * 1024 * 1024   # bytes; a pinning list or cut items for `hub profile pin` (core/profcap.py)
CHECKED_EVERY = 600   # s; how many inbox facts were checked (spool.checked) — no more often, hub stats are sent every 15 min
SNAPSHOT_EVERY = 2000  # events between snapshots in the journal (each is the whole state; it was 200 — 190 MB of 211, 10-01);
                       # no more often than the events since the last one weigh a snapshot (snapshot_due)
PRUNE_AFTER = 26 * 3600  # delivered messages older than this leave the state (core.pruned); the owner's word in chat lives 24 h
PRUNE_EVERY = 3600
STATE_EVERY = 50      # events between state files for commands (journal.save_state)
RETRY_EVERY = 60      # s: a refused state file write or journal rotation (Windows: a reader holds the file) is tried again
GATEWAY_RETRY = 2   # a spawn waiting for the account gateway (it is starting, checking the new claude) — retried after this long
ALIVE = "hubd.alive"  # pulse: hubd touches the file every loop; neighbours (the schedule clock) wait for it until hubd is up
CLEAN_RETRY = (60, 30)   # s between tries, how many: a deleted executor's copy that is an empty shell with a locked root (a program stands in it)
MERGED_RETRY = 60   # experiment, slice C: s — retry the merge check after a git error (a lock, a timeout)
FOLDER_EVERY = 60     # seconds between checks of the specialist's folder (check_folders): git status — not on every tick
FORWARD_RETRY = 3600   # after a failed fast-forward of a lead's folder, the next try (forward_folders)
FORWARD_LOCK_WAIT = 60   # how long a fast-forward waits for another git of the core in the same repo (forward_folders)
MACHINE_EVERY = 30   # how often the hubs of the machine are read again (machine_hubs: the registry, the main hub's folder)
FORWARD_STEP = 1      # how often a held spawn asks whether the core finished fast-forwarding its folder (wait_forward)
UNCOMMITTED_WAIT = 5  # seconds for git status before a mini-hub spawn: the tick waits for it — the spawn matters more than the line
MODS_STEP = 0.5       # seconds between checks whether the modules a session spawn waits for are ready (wait_modules)
RELEASE_STEP = 0.2    # and whether the released process of the same session has exited (wait_release)
RELEASE_LATE = 60     # no fact of its exit by then (the host kills it after 20 s): one host.error, the spawn keeps waiting
CODEX_FOLDER_STEP = 0.5   # and whether a Codex pool account's folder is prepared (wait_codex_folder),
CODEX_FOLDER_WAIT = 120   # at most that long from the first deferral: then the hub folder as its row (CodexHost.folder)
CLAUDE_STEP = 2       # seconds between checks whether Claude Code is installed, while spawns wait for it (wait_claude)
CLAUDE_TELL = 60      # seconds without Claude Code before a hub that worked tells the owner (claude_tick)
CLAUDE_NOTICE = "claude-missing"   # the owner's notice of a hub that worked and lost its Claude Code (wait_claude)
LOGIN_STEP = 5        # seconds between asks whether Claude Code is signed in, while spawns wait for it (wait_login)
# what the waits of a spawn put on its delayed action: not the host's arguments; a spawn held anew decides them anew
WAIT_MARKS = ("mods_since", "private_since", "gateway_since", "gateway_late", "release_since", "release_late",
              "login_wait", "folder_since", "account_fresh")
PRIVATE_EVERY = 2     # whether a spawn waits for the login module (wait_private) — recomputed no more often, for all waiting spawns


in_tree = worktree.in_tree   # path is inside the working tree top (hosts/worktree.py)


def git_failure(e):
    """Why git did not answer — short, for the page and for comparison: the git command and the kind of error, without arguments and paths."""
    cmd = getattr(e, "cmd", None)
    cmd = [str(a) for a in cmd] if isinstance(cmd, (list, tuple)) else []
    if cmd[1:2] == ["-C"]:
        del cmd[1:3]   # git -C <folder> — the path is not shown
    op = "git " + cmd[1] if len(cmd) > 1 else "git"
    if isinstance(e, subprocess.TimeoutExpired):
        return i18n.t("core.daemon.git_timeout", op=op)
    if isinstance(e, FileNotFoundError):
        return i18n.t("core.daemon.git_missing", op=op)
    return f"{op}: {type(e).__name__}"


def req_key(fact):
    """(type, request label data.req) of a fact — for "already recorded" (Hub.recorded_reqs); None if the type or label is not a string:
    those are not put in the set (a wrong shape used to crash hubd on every start) and are not caught as duplicates."""
    if not isinstance(fact, dict) or not isinstance(fact.get("type"), str) or not isinstance(fact.get("data"), dict):
        return None
    req = fact["data"].get("req")
    return (fact["type"], req) if isinstance(req, str) else None


def default_state_dir():
    """HUB_STATE_DIR, else the path from hub-core/state-dir (written by the install: core/statemark), else the
    OS default."""
    if os.environ.get("HUB_STATE_DIR"):
        return os.environ["HUB_STATE_DIR"]
    return statemark.default_state() or platforms.current.state_dir()



class Hub:
    def __init__(self, state_dir, host, registry_dir=None, clock=time.time, rotate_bytes=journal.ROTATE_BYTES,
                 mono=time.monotonic):
        self.state_dir = state_dir
        self.rotate_bytes = rotate_bytes
        self.host = host
        # Claude Code session registry (<pid>.json) — in its folder: the hub's claude_config_dir (catalog.pin_claude_dir) moves it
        self.registry_dir = registry_dir or os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"),
                                                         "sessions")
        self.clock = clock
        self.mono = mono   # the awake clock: does not run while the machine sleeps (macOS, Linux), unlike self.clock
        self.awake_at = None   # (clock, mono) at the last pass: check_slept
        self.after_crash = False   # start(): the previous hubd left with no clean shutdown (until the start-up steps are done)
        self.state_retry = self.rotate_retry = None   # when a refused state file write / journal rotation is tried again
        self.spool_dir = os.path.join(state_dir, "spool")
        os.makedirs(self.spool_dir, exist_ok=True)
        path = os.path.join(state_dir, "journal.jsonl")
        self.carried = journal.carried_bytes(path)
        self.stopped_head = None   # the previous hubd's last journal line, when a rotation at start replaced it (previous_head)
        if os.path.exists(path) and (self.too_big(os.path.getsize(path)) or journal.needs_backfill(state_dir)):
            # the journal grew — the old one goes to the archive whole, the new one starts from a snapshot and a recent tail (journal.rotate)
            head = self.last_head(path)
            try:
                archived = self.rotate(journal.load_state(state_dir, decisions=True))   # the decisions before it, too
                if archived:   # None: no whole event, nothing moved and no "rotate" snapshot — the journal is the old one
                    self.stopped_head = head
                    print(f"hubd: journal moved to {archived}", flush=True)
            except OSError as e:   # Windows: a reader holds the journal — start on it as it is, the tick tries again (rotate_if_big)
                self.rotate_retry = self.clock() + RETRY_EVERY
                print(f"hubd: journal not moved — {e.__class__.__name__}: {e}; retrying in {RETRY_EVERY} s", flush=True)
        self.journal = journal.Journal(path)
        self.snapshot_end = self.journal_size()   # where the events since the last snapshot start (snapshot_due)
        # (type, request label data.req) of the latest facts: an inbox file whose fact was recorded right before hubd died is still
        # in the inbox (spooled). With the type: cli.gave_up carries the label of the command it waited for
        self.recorded_reqs = {req_key(e) for e in journal.read_tail(path, 1 << 20)[-200:]} - {None}
        # (session, mark) of the turns whose start the host put for a cut core hook (collect): that hook, if it outlived
        # its timeout, writes the same start late — it is not written again (spooled)
        self.taken_by_host = {}
        self.taken = set()   # inbox files whose facts are recorded but the files themselves were not deleted (Windows: the file is held)
        self.checked = [0, None]   # inbox facts checked by core/admit.py since the last spool.checked, and since when
        self.state = journal.load_state(state_dir, decisions=True)
        self.spare, self.spare_of = None, None   # spare state for rolling back a fold (fold)
        # hub.json is unreadable at start — default settings, and nothing that takes names from the settings (hub session,
        # system agents, their jobs) is spawned on them until the file is read (settings_known)
        self.config_error = None   # hub.json is unreadable: the error text, to the owner — once (load_catalog)
        self.config = self.read_config()
        self.settings_known = self.config is not None
        self.config = self.config or catalog.defaults()
        self.delayed = []  # (when, action): spawning a crashed hub after a pause
        self.runner = runjobs.Runner(state_dir, self.record, clock=clock)   # `hub run` (core/runjobs.py)
        self.claude_latest = {}   # session -> the token of its newest spawn waiting for Claude Code (wait_claude, wait_login)
        self.claude_release = ownclaude.release()   # the Claude Code this code names: its own copy is taken once there
        self.claude_tokens = itertools.count(1)   # never the same token twice: a dropped wait cannot pass for a newer one
        self.login_next = 0       # a spawn asks Claude Code about its sign-in again no sooner (wait_login)
        self.cut_next = 0         # the next pass over turns cut by a limit (resume_cut)
        self.has_worked = False   # a session of this hub has answered (worked)
        # (session, request number) -> tool input as the session sent it, for a request the journal keeps
        # masked (hold_input): "Allow" hands it out. In memory only: the request ends with the session process
        self.inputs = {}
        # background cleanup of working copies (clean_worktrees): the thread, its results, what it is cleaning now (name -> copy)
        self.cleaner, self.cleaned, self.cleaned_lock, self.cleaning = None, [], threading.Lock(), {}
        self.clean_retry = {}   # copy path -> (tries so far, when to try next by self.mono): in memory only, a restart starts over
        # the merge check in the background — the thread, its results;
        # executor -> (seq of the result, when to retry after a git error or None — checked)
        self.merged_worker, self.merged_done, self.merged_lock, self.merged_seen = None, [], threading.Lock(), {}
        # executor name -> background working copy (start_prepare); repo locks: one at a time per repo
        self.preparing, self.repo_locks, self.repo_locks_lock = {}, {}, threading.Lock()
        self.admitted, self.admit_wait, self.admit_next = set(), set(), {}   # task.admit (admit_pass): let through; ready ones not asked yet; when a held one is asked again
        # fast-forwarding leads' folders (forward_folders): the background check and its results, the git job now, when
        # each lead was last checked, the last reason said to the log
        self.fwd_scan, self.fwd_found, self.fwd_lock, self.fwd_job = None, [], threading.Lock(), None
        self.folders_fwd_at, self.fwd_said = {}, {}
        self.fwd_foreign_said = {}   # lead -> the other hub whose session holds its folder, said to the log
        # several hubs on one machine (core/instance.machine, slice M2): the registry folder and the main hub's state
        # folder (None — the machine's; tests give their own), what machine_hubs read last and when, the folders of the
        # live sessions this hub last published (<state>/folders.json) and when
        self.hubs_registry, self.main_state = None, None
        self.machine, self.machine_at = None, 0.0
        self.published_folders, self.published_at = None, 0.0
        self.next_catalog = 0.0  # a new skill in a set or a module plugin — in the catalog without restarting hubd
        # the watched settings (core/configwatch.py, watch_config): the file marks of the last check, when the next is due
        # anyway, keys that differ from the accepted state -> (the value now, since when)
        self.config_marks, self.config_next, self.config_pending = None, 0.0, {}
        # the app's code layer (core/appwatch.py, watch_app): its folder (False — not a layer), when the next full check is due
        self.app_layer, self.app_next, self.app_full, self.app_mark = None, 0.0, None, 0.0
        self.app_move_next = 0.0   # the move's record from a git clone (core/appmove.py): looked at with app_mark
        # stop between ticks (platforms.on_core_stop) — a list, not an Event: set() from a signal handler
        # could land on a lock held by wait()
        self.stopping = []
        # core modules (core/modrun.py): which processes hub-up keeps and their readiness; without modules — nothing
        self.modules = modrun.Supervisor(state_dir, lambda: self.state, self.record, clock=clock)
        host.modules = self.modules.mods
        # session login (core/profiles.py): the pool in the core tree or the login module; the host gets its own (the pool is read at spawn)
        mine = lambda: self.state.get("providers") or {}   # noqa: E731 — who runs login: hubd state
        self.profiles = profiles.Profiles(state_dir, mods=self.modules.mods, config=lambda: self.config,
                                          clock=lambda: self.clock(), cached=True, ttl=profiles.LIST_TTL,
                                          providers=mine)
        host.profiles = profiles.Profiles(state_dir, mods=self.modules.mods, config=lambda: self.config,
                                          providers=mine)
        # Codex (experimental): the Codex account pool — its module's rows and decisions (an embed's, core/codexslot.py)
        self.codex_pool = codexslot.pool(state_dir, self.modules.mods, lambda: self.config, lambda: self.clock())
        self.codex_logins = None   # the sign-ins last seen: a new one makes the pool's list read again at once
        # who runs session login, the reply voice, the stats report — built-in or a module (core/providers.py)
        self.providers = providers.Tick(self)
        self.profiles.shadow = lambda: self.providers.shadowing()
        self.profiles.observe = lambda *a: self.providers.observe(*a)
        self.corewrite = corewrite.CoreWrite(self)   # modules with core.write: by, handling interception, the pass budget
        self.gateway_down = None   # account gateway (modules/gateway): since when it has not been answering
        self.gateway_fallback = False   # the core switched it off itself (no answer): back — only on a steady pulse
        self.gateway_up = None   # since when the pulse is alive again (after a failure): "steady" is counted from it
        self.gateway_waits = {}   # session -> why its spawn waits for the gateway (one log line per reason)
        # the journal catalog — MCP servers without env and headers (redact.public_catalog); a launch takes the full one that this
        # process built from the settings (load_catalog)
        self.tools = None
        host.tool_flags = lambda **kw: catalog.launch_flags(catalog=self.tools or self.state["catalog"], cfg=self.config,
                                                            state_dir=self.state_dir, **kw)
        host.mcp_servers = lambda **kw: catalog.session_servers(catalog=self.tools or self.state["catalog"],
                                                                cfg=self.config, **kw)   # a grant mid-turn (mcp_set)
        host.session_env = lambda: catalog.session_env(self.config)   # claude variables from the settings (forks)
        host.providers = lambda: self.config.get("session_providers") or {}   # the required login module
        host.hub_settings = lambda: self.config   # rules, critic, "auto-allow": an unreadable hub.json — the previous ones
        host.ask_login = lambda: not self.worked()   # a new hub's spawn first asks Claude Code about its sign-in (wait_login)

    # --- recording a fact ------------------------------------------------------------------------------------
    def snapshot(self, why):
        """A state snapshot by this version of the code — the next start begins from it (see reducer.replay)."""
        if self.spare_of is self.state and self.spare != self.state:   # should not happen: same events — same content
            print("hubd: the spare fold state diverged from the live one — copying again (an edit outside an event?)", flush=True)
        state = {k: v for k, v in self.state.items() if not k.startswith("_")}
        ev = self.journal.append("core.snapshot", data={"why": why, "state": state}, ts=self.clock())
        self.state["seq"] = ev["seq"]
        self.spare_of = None   # the spare is re-copied once per snapshot: a divergence from the live one (should not happen) lives no longer
        self.since_snapshot = 0
        self.snapshot_end = self.journal_size()
        self.save_state()

    def journal_size(self):
        try:
            return os.path.getsize(os.path.join(self.state_dir, "journal.jsonl"))
        except OSError:
            return 0

    def snapshot_due(self):
        """Whether a snapshot is due: None, "periodic" or "rotate". Past SNAPSHOT_EVERY events — and only when the events
        since the last snapshot weigh at least one (the state file is its size); a snapshot that would push the journal
        over the rotation threshold is not written — the rotation starts the new journal with one ("rotate"; None while a
        refused one waits for its retry). Computed afresh every time: a new threshold is not remembered. A crashed hubd still folds from
        state.json (every STATE_EVERY events), not from the last snapshot. 31.10: a 23 MB state was snapshotted every
        2000 events (~3 MB): 73 MB of every 82 MB archive were snapshots, and each periodic snapshot itself pushed the
        journal over too_big — 37 rotations a day, 3 GB of archive."""
        if getattr(self, "since_snapshot", 0) <= SNAPSHOT_EVERY:
            return None
        size = self.journal_size()
        try:
            weight = os.path.getsize(os.path.join(self.state_dir, journal.STATE_FILE))
        except OSError:
            weight = 0
        if size - getattr(self, "snapshot_end", 0) < weight:
            return None
        if not self.too_big(size + weight):
            return "periodic"
        # a refused rotation (Windows: a reader holds the journal) waits for its retry (rotate_if_big): no snapshot meanwhile
        return "rotate" if self.rotate_retry is None else None

    def previous_head(self):
        """The previous hubd's last journal line (last_head): after a rotation at this start the journal ends with the
        "rotate" snapshot stamped now, so it is the line read before the rotation — an empty read stays empty, the new
        journal is no stand-in for it."""
        return self.last_head() if self.stopped_head is None else self.stopped_head

    def unclean(self, head):
        """The last journal event of the previous hubd (previous_head) is not a shutdown snapshot: it left without writing one."""
        return bool(head.strip()) and b'"type":"core.snapshot"' not in head

    def down_since(self, head):
        """When the previous hubd last showed signs of life: the pulse (hubd.alive, every tick) or its last journal event
        (previous_head) — whichever is later; None — first start (the "hub is back" line, owner decision 2026-10-08)."""
        times = []
        try:
            times.append(os.path.getmtime(os.path.join(self.state_dir, ALIVE)))
        except OSError:
            pass
        m = re.search(rb'"ts":\s*([0-9.]+)', head)
        if m:
            times.append(float(m.group(1)))
        return max(times) if times else None

    def last_head(self, path=None):
        """The start of the last journal line (200 bytes: number, time, type)."""
        try:
            with open(path or self.journal.path, "rb") as f:
                end = f.seek(0, 2)
                pos = end - 1  # from the end to the start of the last line: a snapshot is a line of megabytes
                while pos > 0:
                    step = min(65536, pos)
                    f.seek(pos - step)
                    i = f.read(step).rstrip(b"\n" if pos == end - 1 else b"").rfind(b"\n")
                    if i >= 0:
                        pos = pos - step + i + 1
                        break
                    pos -= step
                f.seek(max(pos, 0))
                return f.read(200)
        except OSError:
            return b""

    def save_state(self):
        """The state file for commands. It is only a cache of the journal: a failed write (Windows: a reader held it past the
        replace retries) is a line in the log, not a crash — the old file still matches the journal at its own offset, and the
        next due write or the tick after RETRY_EVERY seconds (retry_state) tries again (2026-11-03: hubd died on it nine times
        a day, and the fleet was brought up each time)."""
        self.since_state = 0
        try:
            journal.save_state(self.state_dir, self.state)
        except OSError as e:
            self.state_retry = self.clock() + RETRY_EVERY
            print(f"hubd: state file not written — {e.__class__.__name__}: {e}; retrying in {RETRY_EVERY} s",
                  flush=True)
        else:
            self.state_retry = None

    def retry_state(self):
        """A refused state file write again, by time: a quiet hub may not reach STATE_EVERY events for hours, and the old file
        (one `hub journal scrub` did not get to clean, for one) would live on."""
        if self.state_retry is not None and self.clock() >= self.state_retry:
            self.save_state()

    def record(self, type, entity=None, data=None, src="core", by=None, deny=None, dedup=None):
        """A fact into the journal. by — a fact of a module with core.write (a label in the line), deny — an inbox rejection without folding the fact
        (core/corewrite.py); dedup — the repeat key of a module fact (hubmod.fact, core/modstate._scope); without modules —
        the previous path."""
        self.since_snapshot = getattr(self, "since_snapshot", 0) + 1
        if self.snapshot_due() == "periodic":
            self.snapshot("periodic")
        ev = self.journal.event(type, entity, data, src=src, ts=self.clock(), dedup=dedup)
        if by is not None:
            ev["by"] = by
        mod = self.corewrite.interceptor(ev) if self.corewrite.on() else None
        if mod is None and by is None and deny is None:
            actions = self.fold_write(ev)[0]
        else:
            actions = self.corewrite.record(ev, mod, deny)
        # the module's own state and readiness — as a slice for its process; modules with rights to parts (sessions, account limits,
        # owner messages — modstate.views) get the parts after every event (the file is written only if they changed)
        try:
            if modstate.changes_slices(type):
                self.modules.publish()
            else:
                self.modules.publish_views()
        except Exception as e:  # noqa: BLE001 — a failure around modules does not crash the core: the tick will try again
            print(f"hubd: module slices — {e.__class__.__name__}: {e}", flush=True)
        self.since_state = getattr(self, "since_state", 0) + 1
        if self.since_state >= STATE_EVERY:
            self.save_state()
        for action in actions:
            self.execute(action)
        return ev

    def rejection(self, ev, why, mod=None):
        """Rejection of fact ev with reason why — as the next number. mod — a module with core.write rejected it (verdict reject)."""
        d = ev["data"]   # who asked and the request number — a rejection the CLI did not wait for will still reach the session
        code = forms.code_of(why)   # the reason code next to the text (core/forms.py): it is parsed by the code
        l10n = coretext.marks(why)   # the core's phrases in the reason: the page, Telegram and the terminal show them in
        # the owner's language, the reason stays as the session reads it (core/coretext.py)
        return self.journal.event("core.rejected", ev["entity"], {"of": ev["seq"], "type": ev["type"], "reason": str(why),
                                                                  **({"code": code} if code else {}),
                                                                  **({"l10n": l10n} if l10n else {}),
                                                                  **{k: d[k] for k in ("req", "sender") if d.get(k)},
                                                                  **({"mod": mod} if mod else {})}, ts=self.clock())

    def fold_write(self, ev):
        """Fold a fact and write it (with a rejection if the core does not accept it) -> (actions, rejection reason)."""
        # the fact and its rejection go in one write: the CLI (journal.decided) must not see a rejected fact without the rejection and
        # print "accepted" (10-14: under load the fold between two writes took longer than its 0.1 s)
        # the write failed — hubd crashes (nobody catches record) and re-reads the journal at start
        self.state, actions, why = self.fold(ev)
        if why:
            rej = self.rejection(ev, why)
            # the actions of folding the rejection (a session note "The core rejected…" when the CLI did not wait, its spawn) — execute them,
            # do not drop them; the rejected fact has no actions of its own
            self.state, actions, _ = self.fold(rej)
            self.journal.write(ev, rej)
        else:
            self.journal.write(ev)
        return actions, why

    def fold(self, ev):
        """reducer.reduce(self.state, ev) without a deep copy of the whole state: the fold is in place, next to it is the spare
        state self.spare, which runs level with the live one (the same event, also in place). A copy per event on a
        big hub is ~60 ms of ~62 (10-22: 1500 deleted sessions, session commands waited seconds for a verdict); this way — two
        in-place folds (~1 ms). On a rejection or a fold failure the spare becomes the live one (the live one may have been modified before the rejection),
        and the spare is re-copied on the next event. A state replaced outside the fold (start, rotation) is recognised
        by the spare by identity and copied anew; a fold that is kept or dropped after checks (core.write — corewrite)
        goes by halves: fold_try, then fold_keep or fold_undo. Actions are copied: the delayed ones (self.delayed)
        stay put while the live state changes. Each gets its own copy of the event: the state shares no objects with the data of whoever called
        record (the fold does not modify the event — the same goes to the journal). Write to self.state only through an event: a change outside
        the fold is invisible to the spare, and a rejection will roll it back (the snapshot compares them — snapshot)."""
        state, actions, why = self.fold_try(ev)
        if not why:
            self.fold_keep(ev)
        return state, actions, why

    def fold_try(self, ev):
        """The live half of fold: ev folded in place into self.state; the spare stays the state before ev until fold_keep
        (the same event into the spare) or fold_undo. Between them the spare is the "before" of a check (corewrite: a fold with a
        module's edits, kept or dropped after checks — without a copy of the whole state per event)."""
        if self.spare_of is not self.state:
            self.spare, self.spare_of = reducer.copy_state(self.state), self.state
        state, actions, why = reducer.reduce(self.state, reducer.copy_state(ev), "accepted")
        if why:   # the live one may have been modified before the rejection — roll back to the spare, the number is the rejected event's (reduce._unchanged)
            return self.fold_undo(ev), [], why
        return state, reducer.copy_state(actions), None

    def fold_keep(self, ev):
        """The spare half of fold: the same event into the spare, level with the live one again."""
        if reducer.reduce(self.spare, reducer.copy_state(ev), "accepted")[2]:
            self.spare_of = None   # the spare diverged from the live one (should not happen) — copy anew on the next event

    def fold_undo(self, ev):
        """Drop what fold_try folded: the spare becomes self.state, with the number of ev (-> it); the spare is re-copied on
        the next event."""
        self.state, self.spare_of = self.spare, None
        self.state["seq"] = ev.get("seq", self.state["seq"])
        return self.state

    def execute(self, action):
        if action.get("delay"):
            self.delayed.append((self.clock() + action["delay"], {k: v for k, v in action.items() if k != "delay"}))
            return
        do = action["do"]
        if do == "owner_notice":   # a module is paused, stopped or failed to start (core/modrun.py) — to the owner
            self.record("owner.notify", None, {"title": action["title"], "text": action["text"], "source": "modules",
                                               "channel": "core", "owner": False,
                                               **({"id": action["id"]} if action.get("id") else {})})
            return
        if do == "milestone_removed":   # the core removed a progress job (core/schedule._drop): the reason into the journal
            self.record("schedule.milestone_removed", action["job"],
                        {"why": action["why"], "n": action["n"], "by": action["by"], "of": action["of"], "channel": "core"})
            return
        if do == "task_note":   # a task trail into its record (core/delegations.trail): a file in the hub folder, not the host
            try:
                lang = i18n.state_lang(self.state) or i18n.SOURCE   # the record is in the hub's language, as before
                taskrec.note(self.state_dir, self.state, action["delegation"], coretext.human(action["who"], lang),
                             coretext.human(action["text"], lang), now=self.clock())
            except Exception as e:  # noqa: BLE001 — a write must not crash the core
                print(f"hubd: task record {action.get('delegation')}: {e!r}", flush=True)
            return
        if do == "board_decision":   # hub decided in a direction's work — into decisions/ of its backlog (core/board.py)
            def write():   # in its own thread: the backlog lock is held by a session command — the core does not wait for it
                try:
                    from modules.directions import direction
                    direction.record_decision(direction.path_of(action["slug"], direction.root(state=self.state_dir)),
                                              action["seq"], action["text"], action["by"], action["ts"])
                except Exception as e:  # noqa: BLE001 — a write must not crash the core
                    print(f"hubd: decision #{action.get('seq')} to backlog {action.get('slug')}: {e!r}", flush=True)
            self.writers = [t for t in getattr(self, "writers", []) if t.is_alive()]
            self.writers.append(threading.Thread(target=write, name=f"decision-{action.get('seq')}", daemon=True))
            self.writers[-1].start()
            return
        if do == "board_decision_revoke":   # hub decided undo: the decision's file leaves the direction's backlog
            # the file of the same decision may still be on its way (its writer waits for the backlog lock): the drop
            # goes after it, or it would find nothing and the file would appear after the withdrawal
            pending = [t for t in getattr(self, "writers", []) if t.name == f"decision-{action.get('seq')}"]

            def drop():
                for t in pending:
                    t.join(30)
                try:
                    from modules.directions import direction
                    direction.drop_decision(direction.path_of(action["slug"], direction.root(state=self.state_dir)),
                                            action["seq"])
                except Exception as e:  # noqa: BLE001 — a write must not crash the core
                    print(f"hubd: withdraw decision #{action.get('seq')} in backlog {action.get('slug')}: {e!r}",
                          flush=True)
            self.writers = [t for t in getattr(self, "writers", []) if t.is_alive()]
            self.writers.append(threading.Thread(target=drop, name=f"decision-drop-{action.get('seq')}", daemon=True))
            self.writers[-1].start()
            return
        if do == "spec_mode":   # spec mode (core/specmode.py): the hook's projection; a dropped spec is put aside
            # the projection first, each step on its own: a failed rename must not keep the edits closed
            for step in (lambda: specmode.write_projection(self.state_dir, action["session"], action),
                         lambda: action.get("dropped") and specmode.drop_file(self.state_dir, action["path"]),
                         # the copy of what the person saw becomes spec.accepted.md
                         lambda: action.get("accepted_from") and specmode.settle_copy(self.state_dir, action["path"],
                                                                                      action["accepted_from"])):
                try:
                    step()
                except OSError as e:   # the hook refuses edits on an unreadable projection; the next launch writes it anew
                    print(f"hubd: spec mode of {action.get('session')}: {e!r}", flush=True)
            return
        if do in ("profile_check", "profile_restore", "profile_pin"):   # an agent's CLAUDE.md cap (core/profcap.py)
            return self.profile_do(action)
        if do == "scrub_journal":   # hub journal scrub --apply: the live journal — by rotation with a scrub in the tick (rotate_if_big)
            self.scrub_due = True
            return
        if do in ("bus_on", "bus_contact"):   # the hub bus: the owner's request decided by the core (core/bus.decide)
            return self.bus_do(action)
        if do in busd.ACTIONS:   # the hub bus: sending, receiving, passing, the liaison (core/busd.py)
            return busd.execute(self, action)
        if do == "mcp_login_pty" or (do == "mcp_callback" and action.get("pty")):
            return self.mcp_pty(action)   # a sign-in the old way (core/mcpwatch): in the background, the tick records the step
        if do == "mcp_callback":   # the address after the sign-in: from the owner's file, once — never in the journal
            url = mcplogin.take_callback(self.state_dir, action.get("attempt"))
            if not url:
                self.record("host.done", action["session"], {"do": do, "attempt": action.get("attempt"),
                                                             "server": action.get("server"), "result": "no-callback"})
                return
            action = {**action, "url": url}
        if do == "spawn":
            action = self.wait_claude(action)   # None — deferred (or superseded); otherwise without its wait mark
            if action is None:
                return
        if do == "spawn":
            action = self.fresh_account(action)   # at every pass of every wait: the pool may have changed since the decision
        if do == "spawn" and (self.wait_release(action) or self.wait_modules(action) or self.wait_private(action)
                              or self.wait_gateway(action) or self.wait_codex_folder(action) or self.wait_codex_login(action)
                              or self.wait_login(action) or self.wait_forward(action)):
            return   # the spawn waits as a delayed action (one delayed queue): Claude Code itself, the previous process,
            # modules with launch.env, login, the gateway, then the sign-in (Codex's or Claude Code's)
        args = {k: v for k, v in action.items() if k != "do" and k not in WAIT_MARKS}
        name = args.pop("session")
        gate = self.state.get("gateway") or {}
        flagged = do == "spawn" and args.pop("gateway", None)
        if flagged and gate.get("on") and not action.get("gateway_late"):
            args["gateway"] = gate["ident"]   # switched off while it waited (or by the core itself: no answer) — directly, as without a gateway
        if do == "spawn" and args.get("minihub"):
            args["uncommitted"] = self.uncommitted(name, args.get("cwd"))
        if do == "spawn" and (args.get("spec") or {}).get("path"):
            try:   # the projection anew before every launch: a restart, a lost write (core/specmode.py)
                specmode.write_projection(self.state_dir, name, args["spec"])
            except OSError as e:
                print(f"hubd: spec mode of {name}: {e!r}", flush=True)
        if do == "spawn" and args.get("resume") and self.codex_left(name, args.get("session_id")):
            args["resume"] = False   # Claude Code starts the conversation afresh with the same --session-id
            print(f"hubd: {name} — its conversation is a Codex thread only, Claude Code starts a new one", flush=True)
        # a reply to a masked request — with the input as is; and to a reply added by a module (core.write) without a mark
        if do == "respond" and (args.pop("masked", False) or (name, args.get("request_id")) in self.inputs):
            args["response"] = self.give_input(name, args.get("request_id"), args["response"])
        try:
            result = getattr(self.host, do)(name, **args)
        except Exception as e:  # the host crashed — that is a fact, not a core failure
            if do == "spawn" and getattr(e, "no_login", False):
                return self.no_login(action)   # Claude Code has no sign-in for it (hosts/streamjson NoLogin): it waits
            # the address after a sign-in carries a one-time code: only the kind of the failure goes to the journal
            self.record("host.error", name, {"do": do, "message": action.get("message"),
                                             "error": type(e).__name__ if do == "mcp_callback" else repr(e)[:300],
                                             **({"attempt": action["attempt"]} if action.get("attempt") else {})})
            return
        if do == "release" and result == "releasing":
            return   # the host waits for the exit in its own thread (hosts/streamjson.RELEASING); host.done release is its
            # fact of the exit, from poll
        if flagged and not args.get("gateway") and result == "spawned":
            result = reducer.GATEWAY_BYPASSED   # spawned directly — drop the gateway mark (otherwise an account switch would count it as on the gateway)
        used = getattr(self.host, "profile_used", None)
        # which agent profile went to the executor (core/execprofile): a digest, with addenda or full, and stale — recompress
        profile = used.pop(name, None) if do == "spawn" and isinstance(used, dict) else None
        # a raise after Opus's filter: whether the host cut the stopped turn out of the conversation (core/refusals.py)
        cuts = getattr(self.host, "refusal_cut", None)
        cut = cuts.pop(name, None) if do == "spawn" and isinstance(cuts, dict) else None
        self.record("host.done", name, {"do": do, "message": action.get("message"),
                                        "attempt": action.get("attempt"), "result": result,
                                        **({"account": action.get("account"), "gen": action.get("account_gen")}
                                           if do == "spawn" and action.get("account_fresh") else {}),
                                        **({"server": action["server"]} if action.get("server") else {}),
                                        **({"profile": profile} if profile else {}),
                                        **({"cut": cut} if cut is not None else {})})

    def profile_busy(self, name, profile):
        """The agent or one of its executors (they carry its profile) is in a turn: an eviction waits."""
        return any(s.get("state") in ("busy", "dialog") and (n == name or s.get("profile") == profile)
                   for n, s in self.state["sessions"].items())

    def profile_do(self, action):
        """After an agent's turn and on its requests: the size of its CLAUDE.md against the cap, an eviction, putting an
        item back, the first pinning (core/profcap.py). Files of the agent's folder only, in the tick (half a megabyte
        of text); a failure is a log line and, for a request, a fact with the reason — the tick lives on."""
        name, prof, do = action["session"], str(action.get("profile") or ""), action["do"]
        sess = self.state["sessions"].get(name)
        if not sess or not prof or reducer.profile_of(sess) != prof:
            return
        folder = os.path.join(self.state_dir, "agents", prof)
        try:
            if do == "profile_restore":
                ok, why = profcap.restore(folder, action["id"], self.clock())
                self.record("core.profile_restored", name, {"id": action["id"], "ok": ok, **({"why": why} if why else {})})
            elif do == "profile_pin":
                spec, cut = (self.pin_file(action.get(k)) for k in ("spec", "cut"))
                got = (profcap.pin(folder, spec, cut, now=self.clock()) if spec is not None and (cut is not None or not
                                                                                                action.get("cut"))
                       else {"ok": False, "problems": ["unreadable"]})
                # counts only: a problem line quotes the list, and the list is a file the caller named
                self.record("core.profile_marked" if got["ok"] else "core.profile_pin_failed", name,
                            {"pinned": got.get("pinned", 0), "seeded": got.get("seeded", 0),
                             "problems": len(got.get("problems") or [])})
            # after a restore no check now: the entry leaves the evicted file at the agent's next turn, once a copy
            # of the file read before the restore can no longer be written back over it
            if do == "profile_check" or do == "profile_pin" and self.state["sessions"][name].get("profile_marked"):
                self.profile_check(name, prof, folder)
            self.profile_failed().pop(name, None)
        except Exception as e:  # noqa: BLE001 — a file of one agent must not stop the core
            if do == "profile_check":   # a waiting eviction that fails is retried once per PROFILE_RETRY, not every tick
                self.profile_failed()[name] = self.clock()
            print(f"hubd: {do} {name}: {e!r}", flush=True)

    def profile_failed(self):
        """Agents whose check failed last -> when (a Hub built in tests by __new__ has no __init__ fields)."""
        return self.__dict__.setdefault("_profile_failed", {})

    def pin_file(self, path):
        """A pinning list or the cut items for `hub profile pin`: a Markdown file (.md) of at most PIN_FILE_MAX bytes,
        else None — the core does not read just any file it is named."""
        if not isinstance(path, str) or not path.endswith(".md"):
            return None
        try:   # a regular file only (not a FIFO, not a device behind a link), read up to the limit
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
            with os.fdopen(fd, "rb") as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                    return None
                raw = f.read(PIN_FILE_MAX + 1)
        except OSError:
            return None
        return raw.decode("utf-8", errors="replace") if len(raw) <= PIN_FILE_MAX else None

    def profile_check(self, name, prof, folder):
        sess = self.state["sessions"][name]
        got = profcap.check(folder, profcap.cap_kb(self.config), self.clock(), marked=bool(sess.get("profile_marked")),
                            busy=self.profile_busy(name, prof))
        if not got:
            return
        if got.get("evicted"):
            self.record("core.profile_evicted", name, {"n": len(got["evicted"]), "ids": got["evicted"],
                                                       "before": got["before"], "after": got["bytes"], "cap": got["cap"]})
        size = {"bytes": got["bytes"], "fixed": got["fixed"], "cap": got["cap"], "pending": bool(got.get("pending"))}
        was = sess.get("profile_size") or {}
        # the journal gets a size when it moved by a KB, or the cap or the waiting changed: not every edit of a line
        key = lambda s: (reducer._kb(s.get("bytes") or 0), reducer._kb(s.get("fixed") or 0), s.get("cap"),  # noqa: E731
                         bool(s.get("pending")))
        if not was or key(was) != key(size):
            self.record("core.profile_size", name, size)

    def profile_pending(self):
        """An eviction that waited (the agent or its executors were busy, the file moved under it): again once nobody of
        the agent is in a turn — without its next turn."""
        for name, sess in list(self.state["sessions"].items()):
            prof = reducer.profile_of(sess)
            failed = self.profile_failed().get(name)
            if failed is not None and self.clock() - failed < PROFILE_RETRY:
                continue
            if prof and (sess.get("profile_size") or {}).get("pending") and not self.profile_busy(name, prof):
                self.profile_do({"do": "profile_check", "session": name, "profile": prof})

    def bus_do(self, action):
        """The owner's request about the bus: keys, self-check, a card — the core's decision as core.bus_* facts. A failure
        is a refused request, not a core crash; ssh-keygen and the library are called here, in the core's process."""
        from core import bus
        try:
            facts = bus.decide(action, self.state.get("bus") or {}, self.state_dir, self.clock())
        except Exception as e:  # noqa: BLE001
            facts = [("core.bus_request_failed", {"of": action.get("of"), "code": "error",
                                                  "reason": f"{type(e).__name__}: {e}"[:300]})]
        before = {n: c.get("status") for n, c in ((self.state.get("bus") or {}).get("contacts") or {}).items()}
        for type_, data in facts:
            self.record(type_, None, data)
        # a contact the owner has just accepted gets our card: its hub shows its owner "this hub asks to be a contact"
        for name, c in ((self.state.get("bus") or {}).get("contacts") or {}).items():
            if c.get("status") == "known" and before.get(name) != "known":
                try:
                    busd.intro_to(self, name)
                except Exception as e:  # noqa: BLE001
                    print(f"hubd: bus, card for {name}: {type(e).__name__}: {e}", flush=True)

    def bus_tick(self):
        """The bus's timers and the liaison's sleep with the bus off (core/busd.tick); nothing without the bus."""
        try:
            busd.tick(self)
        except Exception as e:  # noqa: BLE001 — the bus must not stop the tick
            print(f"hubd: bus, tick: {type(e).__name__}: {e}", flush=True)

    def bus_start(self):
        """With the bus on, the core checks the library, the keys and the self-check at every start (spec hubbus §5.2.1);
        broken — core.bus_key_failed, and a transport does not start on crypto_ready = false. Without the bus nothing
        is imported."""
        if not (self.state.get("bus") or {}).get("enabled"):
            return
        from core import bus
        try:
            facts = bus.start_check(self.state["bus"], self.state_dir)
        except Exception as e:  # noqa: BLE001 — the check must not keep the core from starting
            facts = [("core.bus_key_failed", {"code": "error", "reason": f"{type(e).__name__}: {e}"[:300]})]
        for type_, data in facts:
            self.record(type_, None, data)

    def codex_left(self, name, session_id):
        """Codex (experimental): the hub went back from Codex to Claude Code, and this session's
        conversation is only a Codex thread — `--resume` would fail with "No conversation found" at every start (a crash
        loop). The host that takes the session decides first: a Codex one (resumable not None) knows its own threads;
        for Claude Code — the session id in the session's Codex thread map and no Claude Code transcript of it. A hub
        that never ran Codex has no map: nothing changes for it."""
        if callable(getattr(self.host.__class__, "resumable", None)) and \
                self.host.resumable(name, self.state["sessions"].get(name) or {}) is not None:
            return False
        return codex_only(self.state_dir, name, session_id)

    def wait_claude(self, action):
        """A new hub with no Claude Code yet (the app before «Setup» installed it, modules/setup): the host's program is a
        path (claude_command: `which claude` or ~/.local/bin/claude) to nothing runnable. The spawn waits as a delayed
        action instead of failing — a failed spawn, then «the process is gone», then a respawn counted the hub's own
        session as crashing in a loop. The wait is the core's state (core.claude_missing: restart does not wait for such
        «starting» sessions; a hub that worked is told, claude_tick). Once the program is there (`hub setup claude`, the
        page's «Setup») the spawn goes, without a restart. A newer spawn of the same session (reload, an account, the
        gateway) replaces the waiting one. -> the action to go on with (without its mark), None — deferred or dropped."""
        name, token = action["session"], action.pop("claude_token", None)
        if token is not None and self.claude_latest.get(name) != token:
            return None   # superseded while it waited
        self.claude_own()
        missing = self.claude_program()
        if not missing:
            self.claude_latest.pop(name, None)   # this one goes: a spawn of the session still waiting is stale now
            self.claude_back()
            return action
        if (self.state.get("claude_missing") or {}).get("path") != missing:
            self.record("core.claude_missing", None, {"path": missing})
            print(f"hubd: no Claude Code at {missing} — sessions spawn once it is installed (hub setup claude)", flush=True)
        if token is None:
            token = self.claude_latest[name] = next(self.claude_tokens)
        self.delayed.append((self.clock() + CLAUDE_STEP, {**action, "claude_token": token}))
        return None

    def claude_own(self):
        """The release's own Claude Code (core/ownclaude) came while hubd runs — the update's pass on this release, «Setup»:
        the next spawns go on it without a restart (a running session keeps its program until it respawns), and what the
        host learned from the previous program (`--help`) is asked anew. A stat per spawn; HUB_CLAUDE is left as it is."""
        cmd = getattr(self.host, "claude", None)
        if not self.claude_release or os.environ.get("HUB_CLAUDE") or not callable(getattr(self.host, "claude_from", None)) \
                or not isinstance(cmd, (list, tuple)):
            return
        own = ownclaude.mine(self.state_dir, self.claude_release)
        if not own or list(cmd) == [own]:
            return
        self.host.claude = [own]   # MuxHost (Codex beside claude) passes it to its stream host
        ownclaude.switches()
        ownclaude.touch(self.state_dir, self.claude_release)
        stream = getattr(self.host, "stream", self.host)
        if isinstance(getattr(type(stream), "fresh_prompt", None), property):
            stream.fresh_prompt = None
        print(f"hubd: Claude Code {self.claude_release} of this release is at {own} — new spawns run it", flush=True)

    def worked(self):
        """A session of this hub has answered once (last_out: a result with text, not a failure — «Not logged in» is
        one): its sign-in works, and the core never asks Claude Code about it (wait_login) — whatever `auth status`
        would say of a key only in a project's settings or a login it reports oddly. Once true — for this hubd."""
        if not self.has_worked:
            self.has_worked = any(s.get("last_out") for s in self.state["sessions"].values())
        return self.has_worked

    def wait_login(self, action):
        """A new hub with Claude Code but no sign-in yet (the app before «Setup» signed in): the host asks Claude Code
        before the spawn (hosts/streamjson signed_in) and refuses (no_login) — a session would answer every turn «Not
        logged in». While the core waits (core.login_missing), spawns ask again together once per LOGIN_STEP: the rest
        wait without a program start. A hub whose session answered, a pool account, the gateway, a key in the
        environment are never held (worked, signed_in). True — the spawn is deferred."""
        if not self.state.get("login_missing") or self.clock() >= self.login_next or self.worked() \
                or self.codex_whole():   # a Codex hub asks its own sign-in (wait_codex_login), never Claude Code's
            return False
        self.hold_for_login(action)
        return True

    def wait_codex_folder(self, action):
        """A spawn into a Codex pool account's folder waits while the host prepares it in the background (the hook's
        trust, the Windows sandbox: CodexHost.prepare_codex) — as a delayed action, the tick goes on. At most
        CODEX_FOLDER_WAIT from the first deferral; a row the pool no longer has (removed meanwhile) or a folder that is
        not fit is not waited for — the host takes the hub folder as its row then. True — the spawn is deferred."""
        home = action.get("home")
        # from the host's class: a host that answers every name (a fake with __getattr__) prepares nothing
        if not home or not callable(getattr(type(self.host), "prepare_codex", None)):
            return False
        rows = ((self.state.get("codex") or {}).get("pool") or {}).get("profiles") or []
        if not any(r.get("home") == home and not r.get("hub") for r in rows):
            return False
        ready, why = self.host.prepare_codex(home)
        if ready or why:
            return False
        now = self.clock()
        since = action.get("folder_since", now)
        if now - since >= CODEX_FOLDER_WAIT:
            return False
        self.delayed.append((now + CODEX_FOLDER_STEP, {**action, "folder_since": since}))
        return True

    def wait_forward(self, action):
        """A spawn into a folder the core is fast-forwarding now (forward_folders: the session's hold mark) waits as a
        delayed action: Claude Code would read CLAUDE.md and the settings while git rewrites them. The mark ends with
        the result fact or by itself (reducer.FORWARD_HOLD). True — the spawn is deferred."""
        sess = self.state["sessions"].get(action["session"]) or {}
        if "forwarding" not in sess or not reducer.forwarding(sess, now := self.clock()):
            return False   # the clock only with a mark: the journal of a hub without moves is as before (tests/golden)
        self.delayed.append((now + FORWARD_STEP, action))
        return True

    def wait_codex_login(self, action):
        """Codex (experimental): a hub entirely on Codex with no sign-in yet (codex_signed_in: no sign-in file)
        holds the spawn as a Claude hub holds one without Claude Code's sign-in (no_login: core.login_missing, the same
        delayed queue, restart does not wait for it) — otherwise the session starts and every turn fails with a 401.
        `hub codex login` («Settings → Codex», «Setup») writes the file and the held spawns go by themselves. Asked
        anew at every pass: a stat. Unlike Claude's, a hub whose session answered is held too — `hub codex logout`
        removed the file, nothing to guess. A Claude hub and the claude sessions of a mixed one (HUB_CODEX=1): never.
        True — the spawn is deferred."""
        if not self.codex_whole() or codex_signed_in(self.state_dir, reducer.codex_active_home(self.state)):
            return False
        self.no_login(action, "Codex")
        return True

    def codex_whole(self):
        """The hub entirely on Codex: its settings say so ("host": "codex", as make_host reads them) and its host is the
        Codex one for every session. A host built some other way (a test's own CodexHost) is not held."""
        from core import codexmodel
        return codexmodel.on_codex(cfg=self.config) and getattr(type(self.host), "codex_models", False) is True \
            and getattr(self.host, "all_sessions", False) is True

    def no_login(self, action, what="Claude Code"):
        """The host refused a spawn: Claude Code (or the hub's Codex) has no sign-in for it. The core's state says so
        once; the spawn waits."""
        if not self.state.get("login_missing"):
            self.record("core.login_missing", None, {"missing": True})
            print(f"hubd: {what} is not signed in — sessions spawn once it is (the band of the page, "
                  + ("hub codex login)" if what == "Codex" else "claude auth login)"), flush=True)
        self.login_next = self.clock() + LOGIN_STEP
        self.hold_for_login(action)

    def fresh_account(self, action):
        """A spawn held by any wait decided its pool account earlier than it starts (a pool account added, switched or
        removed while it waited: the sign-in, the previous process, modules, the gateway): the account of the pool now goes
        into its action — the environment the sign-in is asked with, and the one the session starts with. A Codex session
        (its folder) is left as it is."""
        sess = self.state["sessions"].get(action["session"])
        now = reducer.claude_account(self.state, sess) if sess and "home" not in action else None
        if now is None:
            return action
        name, gen = now
        if (name, gen or None) == (action.get("account"), action.get("account_gen")):
            return action
        action = {k: v for k, v in action.items() if k not in ("account", "account_gen")}
        if name:
            action["account"] = name
            if gen:
                action["account_gen"] = gen
        action["account_fresh"] = True   # the session record learns it with the spawn fact (reducer, host.done)
        return action

    def hold_for_login(self, action):
        """Back into the delayed queue with a fresh token: a newer spawn of the session held meanwhile replaces it
        (wait_claude). The other waits' marks stay — their limits count from their first deferral."""
        token = self.claude_latest[action["session"]] = next(self.claude_tokens)
        self.delayed.append((self.clock() + LOGIN_STEP, {**action, "claude_token": token, "login_wait": True}))

    def login_tick(self):
        """The core's «waits for the sign-in» goes when Claude Code said it is signed in (the host asked for a spawn,
        signed_in) or no spawn held for it is left (they spawned, or the sessions went to sleep or were removed). A spawn
        that left the sign-in wait and is held by another one keeps its mark until it reaches the host: no «signed in»
        while it is not."""
        if self.state.get("login_missing") and (getattr(self.host, "login_seen", False) is True
                                                or not any(a.get("login_wait") for _, a in self.delayed)):
            self.record("core.login_missing", None, {"missing": False})
            print("hubd: signed in — sessions spawn", flush=True)

    def claude_program(self):
        """The host's Claude Code when it is a path to nothing runnable -> that path; None — it is there, or a command by
        name (HUB_CLAUDE="claude …": the OS looks it up at spawn, as before). A host made by make_host takes the program
        anew (claude_command): installed elsewhere than the path hubd started with (npm, brew) — spawned from there.
        A stat or two per waiting spawn every CLAUDE_STEP: not cached — a stale «it is there» is the crash loop again."""
        cmd = getattr(self.host, "claude", None)
        missing = None
        if isinstance(cmd, (list, tuple)) and cmd and isinstance(cmd[0], str) and os.path.isabs(cmd[0]) \
                and not claude_present(cmd[0]):
            fresh = getattr(self.host, "claude_from", None)
            fresh = fresh() if callable(fresh) else None
            if fresh and list(fresh) != list(cmd) and claude_present(fresh[0]):
                print(f"hubd: Claude Code is at {fresh[0]} now — sessions spawn it", flush=True)
                self.host.claude = list(fresh)
            else:
                missing = cmd[0]
        return missing

    def claude_back(self):
        """Claude Code is there again: the core's «waits for Claude Code» and the owner's notice go; what the host learned
        from the program (`--help`: a new hub asked it before the install) is asked anew."""
        if not self.state.get("claude_missing"):
            return
        self.record("core.claude_missing", None, {"path": None})
        print("hubd: Claude Code is there — sessions spawn", flush=True)
        if isinstance(getattr(type(self.host), "fresh_prompt", None), property):
            self.host.fresh_prompt = None
        if "n:core:" + CLAUDE_NOTICE in (self.state.get("notices") or {}):
            self.record("owner.notify_resolved", data={"id": CLAUDE_NOTICE, "channel": "core", "owner": False})

    def claude_tick(self):
        """While the core waits for Claude Code, once per CLAUDE_STEP: the program came with no spawn left waiting (the
        sessions went to sleep or were removed meanwhile) — the mark goes; still missing after CLAUDE_TELL on a hub that
        worked (some session started before; a new one has the page's band) — one notice to the owner. Not at once: an
        update of Claude Code swaps its program, and a spawn in that moment must not page the owner."""
        miss = self.state.get("claude_missing")
        if not miss or self.clock() < getattr(self, "claude_next", 0):
            return
        self.claude_next = self.clock() + CLAUDE_STEP
        if not self.claude_program():
            self.claude_back()
            return
        notices = self.state.get("notices") or {}
        if self.clock() - miss["since"] >= CLAUDE_TELL and "n:core:" + CLAUDE_NOTICE not in notices \
                and getattr(self, "claude_told", None) != miss["since"] \
                and any(s.get("ever_started") for s in self.state["sessions"].values()):
            self.claude_told = miss["since"]
            from core import packaged
            self.record("owner.notify", data={
                "title": "Claude Code", "source": "core", "channel": "core", "owner": False, "id": CLAUDE_NOTICE,
                # the app installs it by itself (its band says what goes on); a hub from git installs it with the band's
                # button or the command (the page starts it by itself only for a brand-new hub)
                "text": i18n.t("core.daemon.claude_missing_app" if packaged.is_app(packaged.info())
                               else "core.daemon.claude_missing", path=miss["path"])})

    def wait_release(self, action):
        """The released process of this session (release + spawn: a tools change, a wake right after a stop) or of the
        same conversation under another name (a rename) has not exited yet: the spawn waits for it as a delayed action —
        two processes on one conversation are never alive at once, and the tick does not stand while it exits (the host
        waits for it in its own thread, StreamHost.release). True — the spawn is deferred."""
        # from the host's class: a host that answers every name (a fake with __getattr__) has no released processes
        leaving = getattr(type(self.host), "leaving", None)
        if not callable(leaving) or not self.host.leaving(action["session"], action.get("session_id")):
            return False
        now = self.clock()
        action = {**action, "release_since": action.get("release_since", now)}
        if now - action["release_since"] >= RELEASE_LATE and not action.get("release_late"):
            # the host's thread did not finish (a pipe that does not close): it is said once, the spawn keeps waiting — a second
            # claude on the same conversation, or the old one's end_tree over the new one, would be worse than the wait (in the
            # tick this froze the whole hub)
            action["release_late"] = True
            self.record("host.error", action["session"], {"do": "release", "message": None, "error":
                        f"the session's previous process did not finish exiting within {RELEASE_LATE} s — the new one waits for it"})
        self.delayed.append((now + RELEASE_STEP, action))
        return True

    def wait_modules(self, action):
        """A session spawn waits for modules with launch.env or prompt until they are ready (modrun.Supervisor.pending): after
        a hub restart sessions spawn before the modules answer hello, and would go without their environment and addendum.
        It waits as a delayed action (hubd keeps running and calls hello), no longer than modrun.LAUNCH_WAIT from the first deferral;
        then — without them and a module.error (kind late: it does not count toward the module's pauses) for each; a module that was late no longer
        holds spawns until its next start. Only the stream-json host: other points do not call modules. True — the spawn
        is deferred."""
        if getattr(self.host, "kind", "stream") != "stream":
            return False
        noted = self.__dict__.setdefault("mods_late", {})   # module -> the run in which it was late for a spawn
        try:
            # a module that was late in this run no longer holds spawns — until a new run
            late = [m for m in self.modules.pending()
                    if noted.get(m, False) != (modrun.run_of(self.state_dir, m) or {}).get("launch")]
        except Exception as e:  # noqa: BLE001 — a failure around modules does not hold the spawn
            print(f"hubd: modules before bringing up {action['session']} — {type(e).__name__}: {e}", flush=True)
            return False
        if not late:
            return False
        now = self.clock()
        since = action.get("mods_since", now)
        if now - since < modrun.LAUNCH_WAIT:
            self.delayed.append((now + MODS_STEP, {**action, "mods_since": since}))
            return True
        for m in late:
            noted[m] = (modrun.run_of(self.state_dir, m) or {}).get("launch")
            self.record("module.error", m, {"module": m, "hook": "launch", "kind": "late",
                                            "error": f"not ready to bring up session {action['session']} within "
                                                     f"{modrun.LAUNCH_WAIT:g} s — it was brought up without it",
                                            "channel": "core", "owner": False})
        return False

    def wait_private(self, action):
        """Module extraction spec §4.5: there is no built-in login in the tree (a release without it), the pool is needed, and the login module
        is being installed (the ensure step) or started — the spawn waits instead of going to the machine's login: the conversation continues on the same
        login. No longer than private.PRIVATE_WAIT from the first deferral; then — on the machine's login plus a line to the owner (one per
        hubd life). A built-in login in the tree (this release) — never waits. True — the spawn is deferred."""
        if getattr(self.host, "kind", "stream") != "stream":
            return False
        from modules.update import private
        if private.builtin_present("profile") or not private.load_pin():
            # a built-in login in the tree (this release), or no modules to wait for (the open flavor cuts the pin too):
            # nothing to wait for — and the core clock is untouched
            return False
        now = self.clock()
        seen = getattr(self, "private_seen", None)   # (when, why): deferred spawns repeat every MODS_STEP —
        if seen and 0 <= now - seen[0] < PRIVATE_EVERY:   # the pool and step files are read no more often than PRIVATE_EVERY, for all of them
            why = seen[1]
        else:
            try:
                mods = self.modules.mods
                why = private.waiting(self.state_dir, self.config, ready=lambda n: mods.ready(n) is not None,
                                      status=mods.status)
                self.private_seen = (now, why)
            except Exception as e:  # noqa: BLE001 — a failure around modules does not hold the spawn (and is not remembered)
                print(f"hubd: login module before bringing up {action['session']} — {type(e).__name__}: {e}", flush=True)
                why = None
        if not why:
            return False
        since = action.get("private_since", now)
        if now - since < private.PRIVATE_WAIT:
            self.delayed.append((now + MODS_STEP, {**action, "private_since": since}))
            return True
        told = self.__dict__.setdefault("private_told", set())
        if why not in told:
            told.add(why)
            self.record("owner.notify", None, {"id": "private-wait", "title": i18n.t("core.daemon.private_title"),
                                               "text": i18n.t("core.daemon.private_wait", why=why,
                                                              minutes=private.PRIVATE_WAIT // 60,
                                                              line=private.unavailable_line(self.state_dir, self.config)),
                                               "channel": "core", "owner": False})
        return False

    def wait_gateway(self, action):
        """A spawn on the account gateway waits until the gateway is ready (gateway_ready: it is starting, checking the new claude) — it does not
        start on a stub; if the gateway does not answer for DOWN_WAIT the core switches it off itself (gateway_tick), and the spawn goes
        directly. A gateway that is alive but not ready for longer than DOWN_WAIT (has not checked this claude, the port does not answer) — this spawn goes
        directly (gateway_late), not an endless wait. True — the spawn is deferred."""
        name = action["session"]
        # session login belongs to the login module — the gateway is its too (core/profiles.gateway): whether it is ready is the module's call (not ready — a
        # profile.token reply without gateway, a direct spawn); no need to wait for a built-in one
        wait = self.gateway_ready() if action.get("gateway") and (self.state.get("gateway") or {}).get("on") \
            and self.profiles.gateway() else None
        if not wait:
            self.gateway_waits.pop(name, None)
            return False
        from modules.gateway import gateway as gw   # only the built-in gateway is waited for (core/profiles.gateway)
        now = self.clock()
        since = action.get("gateway_since", now)
        if now - since >= gw.DOWN_WAIT:
            print(f"hubd: {name} waited for the gateway {gw.DOWN_WAIT} s ({wait}) — coming up directly", flush=True)
            self.gateway_waits.pop(name, None)
            action["gateway_late"] = True
            return False
        if self.gateway_waits.get(name) != wait:
            print(f"hubd: {name} is waiting for the gateway: {wait}", flush=True)
        self.gateway_waits[name] = wait
        self.delayed.append((now + GATEWAY_RETRY, {**action, "gateway_since": since}))
        return True

    def uncommitted(self, name, cwd):
        """Uncommitted changes in the mini-hub's folder before a spawn — for its addendum (core/minihub.DIRTY): executors do not
        see it, it hands the sorting out to them, and what belongs to others in a shared folder is not its own. Spawns are rare — always one git status, with a
        short timeout (UNCOMMITTED_WAIT): no answer — the spawn goes without the line and a fact with the reason (the spawn matters more);
        the number is recorded as a fact if it changed (the page mark is fresh since the spawn). The core is stopping — no git.
        -> {"top", "lines", "shared": who else works in the same working tree} or None."""
        if self.stopping:
            return None
        sess = self.state["sessions"].get(name) or {}
        try:
            got = worktree.changes(cwd, timeout=UNCOMMITTED_WAIT)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            why = git_failure(e)
            if why != sess.get("folder_error"):
                self.record("session.folder", name, {"channel": "core", "git": "error", "why": why})
            return None
        if not got:
            return None
        top, lines = got
        had = sess.get("folder_dirty")
        if isinstance(had, bool) or (had or 0) != len(lines) or sess.get("folder_error"):
            self.record("session.folder", name, {"channel": "core", "git": "ok", "dirty": len(lines)})
        if not lines:
            return None
        shared = sorted(n for n, s in self.state["sessions"].items() if n != name and s.get("cwd")
                        and s["state"] not in minihub.GONE and in_tree(s["cwd"], top))
        return {"top": top, "lines": lines, "shared": shared}

    def load_news(self):
        """What changed for sessions (the "for sessions" section of CHANGES.md) — the core attaches it to each session's next message."""
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "CHANGES.md"),
                      encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return
        notes = news.session_notes(text)
        if notes:
            self.record("news.loaded", data={"notes": notes, "heads": news.session_heads(text), "channel": "core",
                                             "owner": False})

    # --- loop ------------------------------------------------------------------------------------------------
    def start(self):
        # since when the core was down and how many messages wait in the inbox — the bot tells the owner (owner decision 2026-10-08)
        head = self.previous_head()   # one read of the journal's end: a stop snapshot is a line of megabytes
        down = self.down_since(head)   # before the pulse below: it is read from the old one
        self.beat()   # the start may take long (a snapshot, git): `hub status` reads the pulse (core/spool.stalled)
        spooled = sum(1 for _, e in spool.take(self.spool_dir)
                      if isinstance(e, dict) and e.get("type") == "message.received")
        self.corewrite.new_pass()
        self.after_crash = self.unclean(head)
        if self.after_crash and self.stopped_head is None:
            # the previous hubd was killed without a snapshot (Mac reboot 10-02): journal readers would rebuild the state from
            # the last snapshot with the new code and see what the core never did — a core snapshot for them. A rotation at
            # this start has written it already: the "rotate" snapshot is the state folded by this code
            self.snapshot("start")
        # i18n: the core knows languages — the fold sets the interface language of a new hub with no history (core/i18n.py human)
        self.record("core.started", data={"pid": os.getpid(), "down_since": down, "spooled": spooled, "i18n": 1,
                                          "host": getattr(self.host, "kind", "stream")})
        self.load_news()
        self.load_catalog()
        self.bus_start()
        # a stop between ticks is visible here too: reap is PowerShell for every live session
        # modules go before the inbox: facts of a module with core.write left while hubd was down, and the interception (core/corewrite.py)
        # module slices go right after want: hub-up starts modules by it, and their first read is already a slice of this life
        try:
            for step in (self.modules.sync, self.modules.publish, self.codex_mark, self.ingest, self.ensure_hub, self.ensure_dev,
                         self.reap, self.reconcile):
                if self.stopping:
                    return
                self.beat()
                step()
        finally:
            self.after_crash = False

    def reap(self):
        """Session processes from hubd's previous life (it was killed without a chance to stop) — kill them before the same sessions are spawned:
        otherwise two processes run one conversation (09-30 18:08)."""
        reap = getattr(self.host, "reap", None)
        for sess in self.state["sessions"].values() if reap else []:
            if sess["state"] in LIVE and reap(sess.get("pid"), sess.get("session_id")):
                print(f"hubd: stopped a leftover process of session {sess['name']} (pid {sess.get('pid')})", flush=True)

    def read_config(self):
        """hub.json now, or None if it is unreadable (a typo, not JSON): the core keeps the previous settings — a core that crashed on
        it was restarted by hub-up in a loop, and the hub stayed down until the file was fixed."""
        try:
            cfg = catalog.load_config(self.state_dir)
        except ValueError as e:
            if str(e) != self.config_error:
                print(f"hubd: settings are unreadable, the core keeps the previous ones: {e}", flush=True)
            self.config_error = str(e)
            return None
        self.config_error = None
        return cfg

    def review_setting(self):
        """hub.json quiet_review → the settings value (owner.review_of), or None (off)."""
        return owner.review_hours(self.config.get("quiet_review"))

    def load_catalog(self):
        cfg = self.read_config()   # and the settings: a new set or project — without a restart
        if cfg is None:
            if self.config_error != getattr(self, "config_told", None):
                self.config_told = self.config_error
                self.record("owner.notify", data={
                    "title": "hub.json", "source": "core", "channel": "core", "owner": False, "id": "config",
                    "text": i18n.t("core.daemon.settings_unreadable", path=catalog.config_path(self.state_dir),
                                   error=self.config_error)})
            if self.tools is not None or not self.settings_known:
                return   # the catalog stays: a typo must not take tools away from sessions
            cfg = self.config   # start on the settings read at startup: MCP server keys come only from them
        else:
            self.config_told = None
            if "n:core:config" in (self.state.get("notices") or {}):   # the file is readable again — the notice is cleared
                self.record("owner.notify_resolved", data={"id": "config", "channel": "core", "owner": False})
        self.config = cfg
        known, self.settings_known = self.settings_known, True
        tz = self.config.get("timezone") or "UTC"
        ci = self.config.get("compact_idle") if isinstance(self.config.get("compact_idle"), dict) else {}
        want = {"timezone": tz, "compact_wake": ci.get("on_wake") is not False,
                "compact_min": ci.get("min_tokens") or parking.COMPACT_MIN_TOKENS}
        # non-working hours (hub.json quiet_review, core/owner.py): off - no field, the journal stays as it was
        review = self.review_setting()
        if review or self.state.get("owner", {}).get("review"):
            want["quiet_review"] = review or False
        caps = executors.caps_from(self.config)   # the overall executor cap: only if set (none by default)
        own = self.state.get("owner", {})
        if caps or own.get("executors"):
            want["executors"] = caps
        drops = executors.drop_tools_from(self.config)   # tools an agent's executors do not inherit (none by default)
        if drops or own.get("drop_tools"):
            want["drop_tools"] = drops
        # parallelism (core/minihub.py) — always: the first event with it marks the boundary of the new rules in the journal
        ex = self.config.get("executors") if isinstance(self.config.get("executors"), dict) else {}
        want["parallel"] = minihub.parallel_of(ex.get("parallel"))
        # the budget gate (core/budget.gate): whether to wait for the owner's word at the limit — yes by default
        bud = self.config.get("budget") if isinstance(self.config.get("budget"), dict) else {}
        want["budget_gate"] = bud.get("owner_gate") is not False
        # MCP servers a sign-in to which waits for the owner's confirmation (core/mcpwatch, owner decision 2026-10-28)
        want["mcp_confirm"] = mcpwatch.confirm_list(self.config.get("mcp_login_confirm"), self.config.get("mcp"))
        if (own.get("tz"), own.get("compact_wake", True), own.get("compact_min") or parking.COMPACT_MIN_TOKENS,
                own.get("executors") or {}, own.get("parallel"), budget.gate(self.state), own.get("mcp_confirm") or [],
                own.get("drop_tools") or {}, owner.review_of(own.get("review"))) != \
                (tz, want["compact_wake"], want["compact_min"], caps, want["parallel"], want["budget_gate"],
                 want["mcp_confirm"], drops, owner.review_of(want.get("quiet_review"))):
            self.record("owner.settings", data=want)
        env = getattr(self.host, "env", None)
        if isinstance(env, dict):   # applies from the next session start
            # Claude Code compacts mid-turn at the hub threshold (compact_idle.mid_turn, off by default): the core
            # compacts only between turns, and a single long turn can grow past the threshold several times
            if ci.get("mid_turn"):
                env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(want["compact_min"])
            else:
                env.pop("CLAUDE_CODE_AUTO_COMPACT_WINDOW", None)
        self.tools = catalog.build(self.config)
        public = redact.public_catalog(self.tools)   # a change of the key alone — no event: the session picks it up
        if public != self.state["catalog"]:          # at the next spawn
            # restarting sessions after a plugin update — only if the owner enabled it (plugin_update_restart)
            self.record("catalog.loaded", data={"tools": public,
                                                **({"restart_updated": True} if self.config.get("plugin_update_restart") else {})})
        self.modules.reload(self.config, self.state["catalog"])   # the module graph — with the catalog (requires.plugins)
        self.settle_admit()   # admit.mode — without a restart, like the other settings
        if not known:   # the first settings read after starting on defaults: whatever takes names from them — spawn it
            self.ensure_hub()
            self.ensure_dev()

    def ensure_hub(self):
        """The hub session is always there (owner decision 2026-09-29): missing or closed — spawn it. Off until the role is approved."""
        if not self.settings_known:
            return   # hub.json is unreadable and was never read: the hub name in it may differ from the default
        hub = self.config["hub"]
        if not hub.get("enabled"):
            return
        sess = self.state["sessions"].get(hub["name"])
        if sess is None or sess["state"] in ("closed", "failed"):
            self.record("session.requested", hub["name"], {
                # the mode is always bypassPermissions (hosts/streamjson.DEFAULT_MODE); hub.mode is not taken from hub.json
                # (owner decision 2026-10-11: an install via Claude Code set another one "for safety"; no choice is needed)
                "cwd": hub.get("cwd"), "session_id": str(uuid.uuid4()), "kind": "hub"})

    def ensure_dev(self):
        """System agents — Hub Developer and Agent Developer (core/system.py): on every start — check they exist, and bring them back."""
        if system.off() or not self.settings_known:
            return
        from core import liaison
        specs = system.specs(self.config, self.state_dir, self.state)
        other = self.state["sessions"].get(liaison.NAME)
        if other is not None and not other.get("liaison") or not callable(getattr(self.host.__class__, "liaison_settings", None)):
            # the reserved name is someone's session: never adopted; a host without the liaison's profile and guard
            # (tmux) does not raise it at all
            specs = [(n, d) for n, d in specs if n != liaison.NAME]
        names, trainer = [name for name, _ in specs], specs[1][0]   # agent-dev — the "Train executors" button
        if (self.state.get("system"), self.state.get("trainer")) != (names, trainer):
            self.record("agents.system", data={"names": names, "trainer": trainer, "channel": "core"})
        for name, data in specs:
            for type, entity, ev in system.returning(self.state, name, data, str(uuid.uuid4()),
                                                     resumable=self.host.resumable if callable(
                                                         getattr(self.host.__class__, "resumable", None)) else None):
                self.record(type, entity, ev)
        self.ensure_jobs()

    def invite_owner(self):
        """A new hub greets its owner first (owner 2026-11-09: a new hub wrote nothing until its owner did): once the
        hub session has come up and nobody has written, the fact owner.invited; the fold gives the hub session the one
        message to introduce itself by ONBOARDING.md. The language is the hub's (hub.json, the owner's words), else the
        system's: a hub nobody has opened yet knows no other. The fold writes it once for the journal."""
        if system.invite_off() or not self.settings_known or not (self.config.get("hub") or {}).get("enabled") \
                or getattr(self, "invite_tried", False) or not system.invite_due(self.state):
            return
        self.invite_tried = True   # the fold writes it once; had it taken no effect, once a start is enough
        lang = i18n.human(self.config, self.state, default=i18n.system_language())
        self.record("owner.invited", None, {"lang": lang, "channel": "core"})

    def ensure_jobs(self):
        """System schedule jobs (core/system.py) — present and not expiring: the check is cheap, every tick."""
        if system.off() or not self.settings_known:
            return
        for type, entity, ev in system.due_jobs(self.state, system.jobs(self.config, self.state_dir), self.clock()):
            self.record(type, entity, ev)
        trainer = self.state.get("trainer") or system.specs(self.config, self.state_dir)[1][0]
        for type, entity, ev in system.rules_review(self.state, self.config.get("rules"), trainer):
            self.record(type, entity, ev)

    def reconcile(self):
        """A session live per the journal but with no process (reboot, kill, OOM) is lost; the core respawns it. An idle one with no work
        is not: it is parked, the first message will spawn it (10-07: after a restart 22 sessions spawned at once)."""
        for name, sess in list(self.state["sessions"].items()):
            if sess["state"] in LIVE and not self.host.alive(name) and name not in self.waiting():
                # there is no process — compaction is no obstacle (otherwise everything spawns at once, which parking protects against)
                if parking.parkable(self.state, sess, gone=True):
                    self.record("session.parked", name, {"channel": "core", "why": "restart"})
                else:
                    # crash: the start follows a stop with no clean shutdown, a lost turn was not cut by a restart (CUT_CRASH)
                    self.record("session.exited", name,
                                {"why": "reconcile", **({"crash": True} if self.after_crash else {})})

    def park_idle(self):
        """Idle for longer than park_after_hours with no work — the process is stopped, the conversation stays (owner decision 2026-10-07)."""
        hours = self.config.get("park_after_hours", parking.PARK_AFTER_HOURS)
        now = self.clock()
        for name, sess in list(self.state["sessions"].items()):
            if parking.idle_too_long(sess, now, hours) and parking.parkable(self.state, sess, now=now):
                self.record("session.parked", name, {"channel": "core", "why": "idle"})

    def check_folders(self):
        """Mini-hubs (core/minihub.py): the specialist's or direction's folder — whether it is not git or a repo without commits ("solo"
        mode) and, for a live mini-hub, how many uncommitted changes it has (git status lines, for the mark; it does not
        hold the transition — the mini-hub hands the sorting out; owner decision 2026-10-16). Once per FOLDER_EVERY s per session and at once when
        its state changed (it became free) or its folder did — first the cheap time check, then git and tasks; the fact
        session.folder — only when something changed or a free session waits for a transition.
        git did not answer (timeout, no git) — a fact with the reason: the folder is unchecked, the tick lives on."""
        if minihub.parallel(self.state) is None:
            return
        now, seen = self.clock(), self.__dict__.setdefault("folders_at", {})
        for name, sess in list(self.state["sessions"].items()):
            if self.stopping:
                return   # git — up to 30 s per call: the rest wait for the next tick
            if not minihub.lead(sess) or sess["state"] in minihub.GONE:
                continue
            key, last = (sess["state"], sess.get("cwd")), seen.get(name)
            if last and last[1] == key and now - last[0] < FOLDER_EVERY:
                continue
            seen[name] = (now, key)
            # git status — only for a live mini-hub, for the mark on the page; not for a sleeping one (the number comes from the spawn,
            # core/daemon.uncommitted), nor for one waiting for a transition: the transition does not wait for it
            ask = minihub.on(self.state, sess) and sess["state"] not in SLEEP
            waits = not ask and not sess.get("minihub_since") and minihub.want(self.state, sess) \
                and minihub.free(self.state, sess)
            try:
                git, dirty = worktree.folder(sess.get("cwd"), dirty=ask)
            except (OSError, subprocess.SubprocessError, ValueError) as e:   # ValueError — output parsing
                why = git_failure(e)
                if why != sess.get("folder_error"):   # the same error — no new fact
                    self.record("session.folder", name, {"channel": "core", "git": "error", "why": why})
                continue
            had = sess.get("folder_dirty")
            moved = dirty is not None and (isinstance(had, bool) or (had or 0) != dirty)
            if minihub.AUTO.get(git) != sess.get("solo_auto") or sess.get("folder_error") or moved \
                    or (git == "ok" and waits):
                # dirty is a field of the new core (a number or null — not counted); the old clean is not written
                self.record("session.folder", name, {"channel": "core", "git": git,
                                                     **({"dirty": dirty} if git == "ok" else {})})

    def forward_on(self):
        """Fast-forwarding leads' folders works here: not on Windows (platforms FORWARDS_FOLDERS: a file held open by
        another process can stop a checkout halfway — after a probe on winstand), not switched off in hub.json
        ("forward_folders": false)."""
        return getattr(platforms.current, "FORWARDS_FOLDERS", False) is True and self.config.get("forward_folders") is not False

    def forward_folders(self):
        """A specialist's or direction's folder stays on the commit it was created from (a direction's copy, the main
        folder of an agent on the default branch: MRs are merged elsewhere), and "Files" shows the old files. The core
        fast-forwards it (`merge --ff-only`, hosts/worktree.forwardable): only a clean folder on the default branch or
        on task/<name> with no commits of its own, to origin/<default> when it is ahead of the local branch, otherwise
        to the local one, no fetch; not the hub's own code (that is the update's), not on Windows (forward_on).
        Each lead once per FOLDER_EVERY s; git runs in the background — a check, then the move: big repos take tens of
        seconds. The move starts only while every live session in the folder is free (minihub.free) and holds them:
        the fact session.folder_forwarding — the fold types nothing to them and the daemon holds their spawn
        (wait_forward) and the executors raised from the folder (forwarding_into); session.folder_forwarded — the
        result, a quiet line to each. One move at a time."""
        job = self.fwd_job
        if job is not None and not job["thread"].is_alive():
            self.fwd_job = None
            ok, why, step = job["result"] or (False, "the background stopped", "error")
            if not ok and step is not None:
                # git refused, timed out or failed: the same every minute would be a fact (and a line) every minute —
                # every lead of the folder tries again in FORWARD_RETRY; what changed since the plan — on the next pass
                for name in job["fact"]["sessions"]:
                    self.folders_fwd_at[name] = self.clock() + FORWARD_RETRY - FOLDER_EVERY
            fact = {**job["fact"], "ok": ok, **({"touched": True} if step == "timeout" else {})}
            # why with the marks of its phrases (why_l10n): the hub show line reads in the owner's language
            self.record("session.folder_forwarded", job["entity"], coretext.put(fact, "why", why) if why else fact)
        self.forward_stale()
        if not self.forward_on():
            return
        if self.fwd_scan is not None and not self.fwd_scan.is_alive():
            self.fwd_scan = None
            with self.fwd_lock:
                found, self.fwd_found = self.fwd_found, []
            for name, plan, why in found:
                if why or self.stopping:
                    # level, and a folder that is no repository: nothing to move, nothing to say
                    if why != self.fwd_said.get(name) and why not in (None, "level", "not_git"):
                        print(f"hubd: the folder of {name} is not fast-forwarded: {why}", flush=True)
                    self.fwd_said[name] = why
                    continue
                if self.fwd_job is not None:
                    self.folders_fwd_at.pop(name, None)   # one move at a time: checked again on the next pass
                    continue
                self.fwd_said.pop(name, None)
                self.forward_start(name, plan)
        if self.fwd_scan is not None or self.fwd_job is not None:
            return
        now, taken, todo = self.clock(), None, []
        for name, sess in self.state["sessions"].items():
            if now - self.folders_fwd_at.get(name, 0) < FOLDER_EVERY or not minihub.lead(sess) \
                    or sess["state"] in minihub.GONE or not sess.get("cwd"):
                continue
            taken = minihub.busy(self.state) if taken is None else taken   # one pass over messages, only when one is due
            if not self.forward_free(sess, taken, now):
                continue   # busy: checked again on the first free tick
            self.folders_fwd_at[name] = now
            # task/<name>, and the branch of its copy while it works in it: renamed, it keeps task/<old name>
            copy = sess.get("worktree") or {}
            branch = (copy.get("branch") or "") if copy.get("path") and in_tree(sess["cwd"], copy["path"]) else ""
            # an instance's own task branch is under its name (hosts/worktree.task_branch): task/<name> is the main hub's
            own = worktree.task_branch(name, self.machine_hubs()["space"])[5:]
            todo.append((name, sess["cwd"], [own] + ([branch[5:]] if branch.startswith("task/") else [])))
        for gone in [n for n in self.folders_fwd_at if n not in self.state["sessions"]]:
            self.folders_fwd_at.pop(gone)
            self.fwd_said.pop(gone, None)
            self.fwd_foreign_said.pop(gone, None)
        if todo:
            self.fwd_scan = threading.Thread(target=self._forward_scan, args=(todo,), name="forward-scan", daemon=True)
            self.fwd_scan.start()

    def _forward_scan(self, todo):
        """The check of forward_folders in the background: git only, the results under the lock."""
        for name, cwd, names in todo:
            if self.stopping:
                return
            try:
                plan, why = worktree.forwardable(cwd, names)
                if plan and worktree.same(plan["top"], catalog.REPO):
                    plan, why = None, "the hub's own code"   # updated by the hub's update, with its self-check
            except Exception as e:   # noqa: BLE001 — a timeout or anything else: the folder is not moved, the log says why
                plan, why = None, f"git: {type(e).__name__}: {e}"[:200]
            with self.fwd_lock:
                self.fwd_found.append((name, plan, why))

    def forward_start(self, name, plan):
        """The move by a plan of the check: every live session of the folder free now (the fold checks it again and
        sets the hold), then git in the background under the repo lock (as start_prepare: one git at a time per repo)."""
        sess, top = self.state["sessions"].get(name), plan["top"]
        if not minihub.lead(sess) or sess["state"] in minihub.GONE:
            return
        inside = sorted(n for n, s in self.state["sessions"].items() if s["state"] not in minihub.GONE and s.get("cwd")
                        and in_tree(s["cwd"], top))
        taken, now = minihub.busy(self.state), self.clock()
        if name not in inside or not all(self.forward_free(self.state["sessions"][n], taken, now) for n in inside):
            return   # a neighbour is working (or the lead left the folder): checked again on the next pass
        machine = self.machine_hubs()
        other = self.foreign_in(machine, top)
        said = self.fwd_foreign_said   # not fwd_said: forward_folders drops it before this call
        if other:   # a session of another hub of the machine lives in the folder: not under it, checked again later
            if said.get(name) != other:
                print(f"hubd: the folder of {name} is not fast-forwarded: a session of {other} works in it", flush=True)
            said[name] = other
            return
        said.pop(name, None)
        fact = {"channel": "core", "path": top, "branch": plan["branch"], "from": plan["head"], "to": plan["target"],
                "ref": plan["ref"], "count": plan["count"], "sessions": inside}
        self.record("session.folder_forwarding", name, fact)
        if not (self.state["sessions"][name].get("forwarding") or {}).get("path") == top:
            return   # the fold refused it
        job = {"entity": name, "top": top, "fact": fact, "result": None}

        def work():
            try:
                common = worktree.common_dir(top)
                repo = common or top
                with self.repo_locks_lock:
                    one = self.repo_locks.setdefault(repo, threading.Lock())
                # the hold of the folder's sessions ends by itself (reducer.FORWARD_HOLD): a copy being checked out
                # in the same repo (start_prepare) is not waited for longer than FORWARD_LOCK_WAIT
                end = time.monotonic() + FORWARD_LOCK_WAIT
                if not one.acquire(timeout=FORWARD_LOCK_WAIT):
                    job["result"] = (False, "another git of the core runs in this repo — next time", None)
                    return
                try:   # and another hub's of the machine (its file lock: only on a machine with more than one hub)
                    with worktree.repo_lock(worktree.lock_target(top, machine["owner"], common),
                                            max(0.0, end - time.monotonic())):
                        job["result"] = worktree.forward(plan)
                except TimeoutError:
                    job["result"] = (False, "another git of a hub of this machine runs in this repo — next time", None)
                finally:
                    one.release()
            except Exception as e:   # noqa: BLE001 — the background is not silent: the result fact carries the reason
                # before the merge (a status timeout, the lock): the files were not touched — forward says when they were
                job["result"] = (False, f"{type(e).__name__}: {e}"[:300], "error")
        job["thread"] = threading.Thread(target=work, name=f"forward-{name}", daemon=True)
        self.fwd_job = job
        job["thread"].start()

    def forward_free(self, sess, taken, now):
        """A session that does not hold the move of its folder: free (minihub.free) and not compacting its
        conversation (/compact reads CLAUDE.md and the memory files again when it ends)."""
        return minihub.free(self.state, sess, taken) and not parking.compacting(sess, now)

    def forward_stale(self):
        """Hold marks no move of this process holds (the core restarted while git ran) — closed with a failed result:
        otherwise the sessions of the folder would wait for reducer.FORWARD_HOLD; while git of the previous process
        still writes the folder's index (index.lock), the mark stays until it ends or the hold runs out."""
        job, stale = self.fwd_job, {}
        for name, sess in self.state["sessions"].items():
            mark = sess.get("forwarding")
            if mark is not None and not (job and (mark or {}).get("path") == job["top"]):
                stale.setdefault((mark or {}).get("path"), []).append(name)
        for path, names in stale.items():
            try:   # the previous process's git may outlive it: while it writes the index, the hold stays (FORWARD_HOLD)
                if path and os.path.isdir(path) and worktree.git_busy(path) and \
                        reducer.forwarding(self.state["sessions"][names[0]], self.clock()):
                    continue
            except (OSError, subprocess.SubprocessError):
                pass
            self.record("session.folder_forwarded", names[0], {"channel": "core", "path": path, "sessions": names,
                                                               "ok": False, "why": "interrupted: the core restarted"})

    def machine_hubs(self):
        """How this hub shares the machine (core/instance.machine): its namespace, its owner mark, the other hubs. Read
        again every MACHINE_EVERY s (an instance added or removed meanwhile); a failure to read is a hub alone — as
        before M2."""
        now = self.mono()
        if self.machine is None or now - self.machine_at >= MACHINE_EVERY or now < self.machine_at:
            from core import instance
            try:
                self.machine = instance.machine(self.state_dir, main_state=self.main_state,
                                                registry=self.hubs_registry)
            except (OSError, ValueError) as e:
                print(f"hubd: the hubs of this machine are not read: {e}", flush=True)
                self.machine = {"space": None, "owner": None, "others": []}
            self.machine_at = now
        return self.machine

    def foreign_in(self, machine, path):
        """The command of another hub of the machine (machine: machine_hubs) one of whose live sessions works in path
        (the folder itself or under it, by its folders.json), or None."""
        from core import instance
        return instance.live_in(machine["others"], path, in_tree)

    def publish_folders(self):
        """<state>/folders.json — the folders of this hub's live sessions for the other hubs of the machine (they do not
        fast-forward, take or remove a copy under them): on a change, and at least every FOLDERS_EVERY s (a file that is
        not rewritten is a stopped hub's). A hub alone on the machine writes nothing."""
        from core import instance
        if not self.machine_hubs()["owner"]:
            return
        cwds = sorted({s["cwd"] for s in self.state["sessions"].values()
                       if s["state"] not in minihub.GONE and isinstance(s.get("cwd"), str) and s["cwd"]})
        now = time.time()   # the readers judge the file by its mtime: the wall clock, not the awake one (a sleep)
        if cwds == self.published_folders and 0 <= now - self.published_at < instance.FOLDERS_EVERY:
            return
        try:
            instance.write_folders(self.state_dir, cwds)
        except OSError as e:
            print(f"hubd: {instance.FOLDERS} is not written: {e}", flush=True)
            return
        self.published_folders, self.published_at = cwds, now

    def forwarding_into(self, base):
        """An executor's base is in the folder the core is fast-forwarding now: it waits (as busy_copy)."""
        job = self.fwd_job
        return job is not None and bool(base) and in_tree(base, job["top"])

    def compact_idle(self):
        """A long conversation of an idle session — compact it while the cache is warm (core/parking.compactable)."""
        cfg, now = self.config.get("compact_idle") or {}, self.clock()
        for name, sess in list(self.state["sessions"].items()):
            if parking.compactable(self.state, sess, now, cfg):
                self.record("session.compact_requested", name, {"channel": "core", "why": "idle"})

    def waiting(self):
        """Sessions whose spawn is deferred: they should have no process."""
        return {a["session"] for _, a in self.delayed}

    def check_slept(self):
        """The machine slept (macOS DarkWake: the hub lives a 10-50 s window every 15 minutes): the wall clock ran ahead of the
        awake clock since the last pass. The tick calls it before every step, so the fact core.slept comes before the next
        step judges a term, also when the machine fell asleep inside a long step (git, a module): the terms of a living
        process (a compaction in progress, the delivery of a message) get the sleep back first. A step that merely took
        long moves both clocks, so it is no sleep."""
        wall, awake = self.clock(), self.mono()
        last, self.awake_at = self.awake_at, (wall, awake)
        if last and (gap := (wall - last[0]) - (awake - last[1])) >= SLEPT_MIN:
            self.record("core.slept", data={"seconds": round(gap)})

    def beat(self):
        path = os.path.join(self.state_dir, ALIVE)
        try:
            os.utime(path)
        except FileNotFoundError:
            open(path, "w").close()

    def tick(self):
        """The tick steps. The core sees a stop between ticks (self.stopping) both between steps and between the items of long steps
        (git — up to 30 s per call, host.kill — up to 20 s): the snapshot is written after the tick, and the tick must not eat the time
        hub-up gives the core to stop."""
        self.corewrite.new_pass()   # the budget of modules with core.write — per tick pass
        for step in (self.beat, self.observe, self.check_starting, self.check_mcp, self.mcp_login_tick, self.modules.tick,
                     self.providers_tick, self.fire_timers,
                     self.restart,
                     self.budget_mode,   # limit-saving mode — before the queue: a window reset lifts it in the same tick
                     self.revive_asks,   # stale cards wake their sessions (not in the limit-saving mode: after budget_mode)
                     self.resume_cut,   # sessions cut by a limit go before the executors queue
                     self.forward_folders,   # before the executors: one raised from a lead's folder starts from the fresh base
                     self.run_tasks,   # executors for a task (spec, release 2)
                     self.runs_tick,   # `hub run`: start, pick up, outcome, stop (core/runjobs.py)
                     self.check_merged,   # experiment, slice C: before retirement — a reminder wakes one not asleep yet
                     self.retire_executors, self.clean_worktrees, self.prune_due, self.reload_catalog_if_due,
                     self.watch_config,   # after the settings re-read: the check sees the fresh hub.json
                     self.watch_app,
                     self.run_delayed, self.rotate_if_big, self.retry_state, self.compact_idle, self.park_idle, self.switch_accounts,
                     self.gateway_tick, self.claude_tick, self.login_tick, self.codex_tick, self.ensure_jobs, self.invite_owner, self.check_folders,
                     self.count_checked, self.bus_tick,
                     self.profile_pending,   # an eviction that waited for the agent and its executors to be free
                     self.publish_folders):   # several hubs on one machine: the folders of the live sessions
            if self.stopping:
                return
            self.check_slept()   # before every step: a sleep inside the previous one (git, a module) is known before the next judges a term
            step()

    def runs_tick(self):
        self.runner.tick(self.state)

    def check_starting(self):
        """The process is alive but there has been no SessionStart for longer than start_stuck_minutes (hub.json; 0 by default — do not watch, 10 advised) —
        the spawn is stuck (after an account switch the process is written to the registry and stays silent): the fact session.start_stuck, the automaton kills
        the process and respawns the same conversation. A spawn waiting in the queue (self.delayed) and a session with no process (reconcile
        will pick it up) are not stuck."""
        minutes = self.config.get("start_stuck_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
            return   # absent, 0 or not a number ("10" as a string) — do not watch: the tick must not crash on a hub.json typo
        now, delayed = self.clock(), self.waiting()
        for name, sess in list(self.state["sessions"].items()):
            if self.stopping:   # kill — up to 20 s per call: a core stop between ticks is visible between sessions too
                return
            if sess["state"] != "starting" or name in delayed:
                continue
            since = max(sess["since"], sess.get("spawned") or 0)   # from the real spawn (reducer, host.done spawn)
            if now - since >= minutes * 60 and self.host.alive(name):
                self.record("session.start_stuck", name, {"minutes": round((now - since) / 60)}, src="observer")

    def check_mcp(self):
        """MCP status of live sessions once per mcpwatch.EVERY (core/mcpwatch): a question straight to the host — observation, like observe;
        the answer arrives as a session.mcp fact if there is something to say."""
        ask = getattr(self.host, "mcp_status", None)
        if not ask:
            return
        asked, now = self.__dict__.setdefault("mcp_asked", {}), self.clock()
        for name in mcpwatch.due(self.state, asked, now):
            asked[name] = now
            try:
                ask(name)
            except Exception as e:  # noqa: BLE001 — the process left between ticks: its respawn goes its own way
                print(f"hubd: MCP status {name}: {e!r}", flush=True)

    def mcp_login_tick(self):
        """A sign-in from the page (core/mcpwatch): its end is announced by nothing — the attempt's session is asked
        for the status once per mcpwatch.POLL while the attempt is open; and the steps of sign-ins the old way (pty),
        done in the background, go to the journal here, in the tick."""
        poll, now = getattr(self.host, "mcp_login_poll", None), self.clock()
        polled = self.__dict__.setdefault("mcp_polled", {})
        live = mcpwatch.polling(self.state, now) if poll else []
        for a in live:
            if now - polled.get(a["id"], -mcpwatch.POLL) >= mcpwatch.POLL:
                polled[a["id"]] = now
                try:
                    poll(a["session"], server=a["server"], attempt=a["id"])
                except Exception as e:  # noqa: BLE001 — the process left: the attempt fails by its state change
                    print(f"hubd: MCP sign-in {a['server']} through {a['session']}: {e!r}", flush=True)
        for k in [k for k in polled if k not in {a["id"] for a in live}]:
            del polled[k]
        # the old way with the browser on the hub's machine: it comes back to `claude mcp login`'s own listener and the
        # waiting process ends without an address from the page — its result is the end of the attempt
        seen = self.__dict__.setdefault("mcp_pty_seen", {})
        for a in [a for a in (self.state.get("mcp_logins") or {}).values() if isinstance(a, dict) and a.get("pty")
                  and a.get("state") == "open" and 0 <= now - (a.get("ts") or 0) < mcpwatch.LIFE + 60]:
            if now - seen.get(a["id"], -mcpwatch.POLL * 2) < mcpwatch.POLL * 2:
                continue
            seen[a["id"]] = now
            end = mcplogin.ended(mcplogin.work_dir(self.state_dir, a["server"]))
            if end is not None:
                self.record("session.mcp_login", a["session"], {"attempt": a["id"], "server": a["server"], "pty": True,
                                                                "step": "callback", **mcplogin.outcome(*end)})
        for k in [k for k in seen if now - seen[k] > mcpwatch.LIFE + 120]:
            del seen[k]
        jobs = self.__dict__.setdefault("mcp_jobs", [])
        for job in [j for j in jobs if j["done"].is_set()]:
            jobs.remove(job)
            self.record("session.mcp_login", job["session"], {"attempt": job["attempt"], "server": job["server"],
                                                              "pty": True, **job["result"]})

    def mcp_pty(self, action):
        """A sign-in the old way — `claude mcp login --no-browser` in a pseudo-terminal (core/mcplogin), when no live
        session can sign in by control requests: the link (up to a minute) and then the address after the sign-in (up
        to a minute) in a thread; the result is a session.mcp_login fact in the next tick (mcp_login_tick)."""
        name, server, attempt = action["session"], action["server"], action["attempt"]
        url = mcplogin.take_callback(self.state_dir, attempt) if action["do"] == "mcp_callback" else None
        sess = self.state["sessions"].get(name) or {}
        job = {"session": name, "server": server, "attempt": attempt, "done": threading.Event(), "result": None}

        def link():
            cwd = mcplogin.login_dir(self.state_dir, name, server) or sess.get("cwd")
            got, why = mcplogin.start(server, cwd, mcplogin.work_dir(self.state_dir, server), say=lambda _s: None)
            if got:
                mcplogin.put_link(self.state_dir, attempt, got)
            return {"step": "auth", "ok": bool(got), **({} if got else {"error": str(why or "")[-300:]})}

        def callback():
            if not url:
                return {"step": "callback", "ok": False, "error": i18n.t("core.daemon.mcp_no_callback")}
            work = mcplogin.work_dir(self.state_dir, server)
            code, out = mcplogin.finish(work, url)
            if code != 0 and (mcplogin.ended(work) or (None,))[0] == 0:
                code = 0   # the browser came back to the listener a moment before the address: the login went through
            return {"step": "callback", **mcplogin.outcome(code, out)}

        def work():
            try:
                job["result"] = (link if action["do"] == "mcp_login_pty" else callback)()
            except Exception as e:  # noqa: BLE001 — the background is not silent: the page gets the reason
                job["result"] = {"step": "auth" if action["do"] == "mcp_login_pty" else "callback", "ok": False,
                                 "error": type(e).__name__}
            job["done"].set()
        self.__dict__.setdefault("mcp_jobs", []).append(job)
        threading.Thread(target=work, name=f"mcp-login-{attempt}", daemon=True).start()

    def journal_settings(self):
        """The journal section of hub.json (rotate_mb, compact_on_rotate) — re-read with the settings, without a restart."""
        cfg = getattr(self, "config", None)
        if cfg is None:   # at start there are no settings yet
            try:
                cfg = catalog.load_config(self.state_dir)
            except ValueError:
                cfg = {}
        return cfg["journal"] if isinstance(cfg.get("journal"), dict) else {}

    def limit(self):
        """The rotation threshold: journal.rotate_mb from hub.json ("Settings -> General -> Advanced", owner decision 2026-10-11), else
        rotate_bytes (journal.ROTATE_BYTES by default)."""
        mb = self.journal_settings().get("rotate_mb")
        return int(mb) << 20 if isinstance(mb, (int, float)) and not isinstance(mb, bool) and 10 <= mb <= 100 \
            else self.rotate_bytes

    def too_big(self, size):
        """Over the threshold — and over what the previous rotation carried by half the threshold: a rotation that cannot
        shrink the journal is not repeated on every event (10-10: 21.4 MB carried at a 20 MiB threshold — ~5000
        identical copies in the archive, 120 GB in 13 hours)."""
        limit = self.limit()
        return size > limit and size > self.carried + limit // 2

    def rotate(self, state, scrub=False):
        """journal.rotate and the size of what was carried — for too_big; the same at start and on the fly. An OSError is a
        refused rotation (the callers retry it) only while the journal is the old file: one after the replace is not — the
        Journal object and the state still belong to the old file, and the next event would repeat a seq of the new one;
        it fails hubd as before, and hubd is brought up on the new journal."""
        path = os.path.join(self.state_dir, "journal.jsonl")
        before = journal.identity(path)
        try:
            archived = journal.rotate(self.state_dir, state, scrub=scrub,
                                      compact_previous=self.journal_settings().get("compact_on_rotate") is True)
            self.carried = self.snapshot_end = os.path.getsize(path)
        except OSError as e:
            if journal.identity(path) == before:
                raise
            raise RuntimeError(f"the journal was replaced, the rotation is not finished: {e!r}") from e
        self.since_snapshot = 0   # the new journal starts with a snapshot
        if self.carried > self.limit():
            print(f"hubd: journal after rotation is {self.carried >> 20} MiB — above the threshold {self.limit() >> 20} MiB; "
                  "the next one — when it grows by another half of the threshold", flush=True)
        return archived

    def rotate_if_big(self):
        """The journal is over rotate_bytes — a new one right on the fly, as at start (journal.rotate): the old one goes to the archive whole,
        the new one is a snapshot and a recent tail. Readers (the page, the clock) see the new file: they finish the archive and move on (core/tail).
        10-07, owner: the journal had to be compacted urgently, it was already slow — 61 MB, while rotation happened only at hubd start."""
        path = os.path.join(self.state_dir, "journal.jsonl")
        retry = self.rotate_retry is not None   # a refused one is due whatever asked for it (the size, a scrub, the backfill at start)
        if retry and self.clock() < self.rotate_retry:   # refused a moment ago: not on every tick
            return
        scrub, self.scrub_due = getattr(self, "scrub_due", False), False
        try:
            if not scrub and not retry and not self.too_big(os.path.getsize(path)) and self.snapshot_due() != "rotate":
                return
        except OSError:
            return
        if scrub:   # hub journal scrub: the state is re-read from the new journal already scrubbed — the input of waiting
            self.hold_pending()   # requests goes here as is first, otherwise "Allow" on them becomes a rejection
        # no snapshot into the old journal first: the new one starts with the same state ("rotate"), and a second copy of a
        # whole state in every archive was a third of it (31.10)
        try:
            archived = self.rotate(self.state, scrub=scrub)
        except OSError as e:   # Windows: a reader or an antivirus holds the journal past the retries — it stays as it was
            self.rotate_retry, self.scrub_due = self.clock() + RETRY_EVERY, scrub
            print(f"hubd: journal not moved — {e.__class__.__name__}: {e}; retrying in {RETRY_EVERY} s", flush=True)
            return
        self.rotate_retry = None
        self.journal = journal.Journal(path)
        self.state = journal.load_state(self.state_dir)
        self.save_state()
        print(f"hubd: journal moved to {archived} on the fly", flush=True)

    def prune_due(self):
        if self.clock() >= getattr(self, "next_prune", 0):
            self.prune()

    def reload_catalog_if_due(self):
        """Once per CATALOG_EVERY — the sets on disk; an edit of hub.json (hub tools import) — at once: the CLI waits for it in the catalog."""
        try:
            mtime = os.stat(catalog.config_path(self.state_dir)).st_mtime_ns
        except OSError:
            mtime = None
        if self.clock() >= self.next_catalog or mtime != getattr(self, "config_mtime", mtime):
            self.next_catalog = self.clock() + CATALOG_EVERY
            self.load_catalog()
        self.config_mtime = mtime

    def watch_app(self):
        """The app's code layer (core/appwatch.py): at the first tick — the core is up (the launcher's start record), then
        the full check at once and every FULL_EVERY, and a restore the launcher left; a fact only when what the state says
        changes. A hub from git or an older package — nothing, ever (looked up once)."""
        if self.app_layer is None:
            from core import packaged
            self.app_layer = (packaged.is_app(packaged.info()) and appwatch.layer(packaged.REPO)) or False
        if not self.app_layer:
            return
        name, now = os.path.basename(self.app_layer), self.clock()
        if now >= self.app_mark:   # and again later: the shell may start again while the core runs (a menu entry)
            self.app_mark = now + appwatch.MARK_EVERY
            try:
                appwatch.came_up(self.state_dir, name)
            except OSError:
                pass   # the launcher's record only: its watchdog then puts the release back on an edited layer
        if now >= getattr(self, "app_move_next", 0.0):   # the move's record (core/appmove.py): once a minute — the
            self.app_move_next = now + appwatch.MARK_EVERY   # shell marks it done only after the page answered
            move = appwatch.moved(self.state_dir, self.state)
            if move:
                self.record("app.moved", data=move)
        back = appwatch.restored(self.state_dir, self.state, name)
        if back:
            self.record("app.code_restored", data=back)
            self.app_next = 0.0   # what the layer is now — at once
        if now < self.app_next:
            return
        first = self.app_full is None   # the start: the quick check (sizes and times), the sha256 once a day
        full = not first and now >= self.app_full
        found = appwatch.check(self.app_layer, full=full)
        if found is None:   # not readable now: in a minute
            self.app_next = now + appwatch.MARK_EVERY
            return
        if first or full:
            self.app_full = now + (appwatch.FULL_FIRST if first else appwatch.FULL_EVERY)
        self.app_next = self.app_full
        if appwatch.news(self.state, found):
            self.record("app.code_modified", data=found)

    def watch_config(self):
        """Settings that change how the core works (core/configwatch.py), against the accepted state: the first check
        after an update accepts everything as it is; a key the page saved is accepted silently; any other change that
        holds for SETTLE seconds is config.changed_outside. Checked when hub.json or the owner's settings.json changed,
        while something is pending, and once per EVERY. hub.json unreadable — no check; settings.json unreadable — its
        values stay as accepted, hub.json is still compared. A failed read is tried again in SETTLE seconds."""
        if not self.settings_known or self.config_error:
            return
        now, seen = self.clock(), self.state.get("config_seen")
        path = os.path.join(catalog._claude_dir(), "settings.json")
        try:
            mtime = os.stat(path).st_mtime_ns
        except OSError:
            mtime = None
        marks = (getattr(self, "config_mtime", None), path, mtime)
        if seen is not None and marks == self.config_marks and now < self.config_next and not self.config_pending \
                and not seen.get("page"):
            return
        # the file as written, not over the core's defaults: a default a release adds is not an edit outside the page
        try:
            raw = catalog.read_settings(catalog.config_path(self.state_dir))
        except (OSError, ValueError):
            raw = None
        own = None if not isinstance(raw, dict) else configwatch.read_settings(
            path, configwatch.env_names(raw, catalog.USER_ENV), configwatch.user_sources(raw))
        # a failed read waits for a file to change (a write in progress ends with a new mtime) or for EVERY
        self.config_marks, self.config_next = marks, now + configwatch.EVERY
        if own is None and (seen is None or not isinstance(raw, dict)):
            return
        if own is None:   # settings.json does not parse: the values it still gives as accepted, the rest compared
            names, user = configwatch.env_names(raw, catalog.USER_ENV), configwatch.user_sources(raw)
            own = {k: v for k, v in seen["values"].items() if configwatch.selected(k, names, user)}
            cur = {**configwatch.snapshot(raw, {}), **own}
        else:
            cur = configwatch.snapshot(raw, own)
        if seen is None:
            self.record("config.seen", data={"values": cur, "first": True})
            return
        diff = configwatch.changed(seen["values"], cur)
        marked = seen.get("page") or {}
        if marked:   # the page saved these keys: its value is already in the file (written before the event)
            mine = [k for k in diff if configwatch.covered(k, marked)]
            self.record("config.seen", data={"values": {k: cur.get(k) for k in mine}, "page": sorted(marked)})
            diff = [k for k in diff if k not in mine]
        for key in [k for k in self.config_pending if k not in diff]:
            del self.config_pending[key]
        for key in diff:
            value, since = self.config_pending.get(key, (None, None))
            if since is None or value != cur.get(key):
                self.config_pending[key] = (cur.get(key), now)
            elif now - since >= configwatch.SETTLE:
                del self.config_pending[key]
                old, new = seen["values"].get(key), cur.get(key)
                self.record("config.changed_outside", data={
                    "key": key, "old": old, "new": new,
                    **({"hidden": True} if configwatch.hidden(old) or configwatch.hidden(new) else {})})

    def run_delayed(self):
        """Delayed actions (spawning a crashed hub after a pause). The time is taken on every pass (10-05: it was taken only when
        the catalog was re-read, and a delayed spawn on another pass crashed hubd in a loop)."""
        now = self.clock()
        due = [a for t, a in self.delayed if t <= now]
        self.delayed = [(t, a) for t, a in self.delayed if t > now]
        for action in due:
            if self.state["sessions"].get(action["session"], {}).get("state") == "starting":
                self.execute(action)
            elif action.get("claude_token") is not None and \
                    self.claude_latest.get(action["session"]) == action["claude_token"]:
                del self.claude_latest[action["session"]]   # it waited for Claude Code and left «starting» meanwhile

    def prune(self):
        """The state does not grow forever: handed-in tasks of long-deleted executors go to the archive (the full task text is
        in its record on disk), long-delivered messages go out (they are in the journal)."""
        self.next_prune = self.clock() + PRUNE_EVERY
        ids = [did for did in delegations.archivable(self.state, self.clock() - delegations.ARCHIVE_AFTER)
               if self.recorded(did)]
        if ids:
            self.record("core.archived", data={"ids": ids})
        before = self.clock() - PRUNE_AFTER
        if reducer.prunable(self.state, before):
            self.record("core.pruned", data={"before": before})

    def recorded(self, did):
        """The task record tasks/<id>.md exists and holds the whole context (if not — append it verbatim): after archiving only the
        beginning stays in the state. If it cannot be written — the task does not go to the archive."""
        d = self.state["delegations"][did]
        if d.get("trimmed"):   # was already archived (the executor was brought back): the full text was written then
            return True
        try:
            return taskrec.add_context(self.state_dir, self.state, did, d.get("context") or "")
        except (OSError, ValueError) as e:
            print(f"hubd: task record {did} — {e}; not going to the archive", flush=True)
            return False

    def clean_worktrees(self):
        """Working copies of deleted executors — remove them without losing work (hosts/worktree.remove). git runs in the
        background: a big copy takes 17-20 s each, and the tick stood still (10-08, 10-14: a batch of `hub session remove` — each
        command waited longer than the CLI's 5 s, sessions saw "in the inbox" and counted the command lost). The next tick
        writes the result — if the session is still that deleted one, with the same copy (it could have been brought back or recreated under the same
        name); while the background is busy — start no new ones, and an executor onto the same copy waits (busy_copy)."""
        alive = self.cleaner is not None and self.cleaner.is_alive()   # before taking the results: it has already handed over its own
        with self.cleaned_lock:
            done, self.cleaned = self.cleaned, []
        for name, path, since, ok, why in done:
            sess = self.state["sessions"].get(name) or {}
            wt = sess.get("worktree") or {}
            if sess.get("state") == "removed" and sess.get("since") == since and wt.get("path") == path \
                    and not wt.get("cleaned"):
                # why with the marks of its phrases (why_l10n): the note about a kept copy reads in the owner's language
                self.record("session.worktree_removed", name, coretext.put({"ok": ok}, "why", why))
        if alive:
            return
        self.cleaning = {}
        todo = []
        waiting = executors.worktrees_to_clean(self.state)
        now = self.mono()
        self.clean_retry = {p: v for p, v in self.clean_retry.items() if any(s["worktree"]["path"] == p for s in waiting)}
        for sess in waiting:
            if self.stopping:
                return   # the next run will clean the rest
            if sess["name"] in self.preparing:
                continue   # a copy is being checked out again under this name (start_prepare) — the same folder, no longer for cleanup
            path = sess["worktree"]["path"]
            if self.clean_retry.get(path, (0, 0))[1] > now:
                continue   # an empty shell that did not go: its next try is not yet
            # someone else works in the copy (it was given to an executor with --here) — the copy is his, not for cleanup
            who = next((n for n, s in self.state["sessions"].items() if n != sess["name"] and s.get("cwd")
                        and s["state"] not in minihub.GONE and in_tree(s["cwd"], path)), None)
            if who:
                self.record("session.worktree_removed", sess["name"], coretext.put({"ok": False}, "why", coretext.T(
                    "core.daemon.copy_in_use", "{who} works in the copy — I keep it").format(who=who)))
            else:
                todo.append((sess["name"], sess.get("since"), dict(sess["worktree"])))
        if todo:
            self.cleaning = {name: info for name, _, info in todo}
            self.cleaner = threading.Thread(target=self._clean, args=(todo, self.machine_hubs()),
                                            name="clean-worktrees", daemon=True)
            self.cleaner.start()

    def busy_copy(self, name, run, base):
        """An executor onto a copy the background is cleaning now — wait a tick: under the same name its copy is the same
        folder, `--here` is right inside it (git would delete it from under the just-spawned one), `--branch` — its branch is still checked out
        in the copy being cleaned."""
        if self.cleaner is None or not self.cleaner.is_alive():
            return False
        return name in self.cleaning or any(run.get("here") and in_tree(base, info["path"]) or
                                            run.get("branch") and run["branch"] == info.get("branch")
                                            for info in self.cleaning.values())

    def _clean(self, todo, machine=None):
        """The clean_worktrees background: does not touch the state, only git and the results list (under a lock: taking the results swaps
        the list). Several hubs on one machine (machine_hubs): another hub's copy stays, git takes the repo's file lock — a
        copy whose repo another hub's git holds longer than COPY_TIMEOUT is not cleaned now and goes again next run."""
        machine = machine or {"owner": None, "others": []}
        owner = machine["owner"]
        foreign = (lambda path: self.foreign_in(machine, path)) if owner else None
        held = set()   # repos whose lock another hub held past the wait: their other copies — next run, no new wait
        for name, since, info in todo:
            if self.stopping:
                return   # the ones not cleaned — again on the next run (cleaned is not recorded)
            target = worktree.lock_target(info.get("repo"), owner)
            if target in held:
                continue
            try:
                with worktree.repo_lock(target, worktree.COPY_TIMEOUT):
                    # a hub alone on the machine: the call as before M2 (no new arguments)
                    ok, why = worktree.remove(info, owner=owner, foreign=foreign) if owner else worktree.remove(info)
            except TimeoutError:
                print(f"hubd: the copy of {name} waits for git of another hub in its repo — next run", flush=True)
                held.add(target)
                continue
            except Exception as e:   # noqa: BLE001 — the background does not fail silently: a result with the reason, the copy stays
                ok, why = False, coretext.T("core.daemon.cleanup_failed", "the cleanup did not work: {error}").format(
                    error=f"{type(e).__name__}: {e}")
            if ok is None:   # an empty shell whose folder does not go now: no result yet, a bounded number of tries
                every, tries = CLEAN_RETRY
                done = self.clean_retry.get(info["path"], (0, 0))[0] + 1
                if done < tries:
                    self.clean_retry[info["path"]] = (done, self.mono() + every)
                    continue
                ok = False   # the last try: the reason goes to the hub as it is
            self.clean_retry.pop(info["path"], None)
            with self.cleaned_lock:
                self.cleaned.append((name, info["path"], since, ok, why))

    def check_merged(self):
        """An executor's result — whether the branch of
        its copy is merged into the base (hosts/worktree.merged); not — the fact executor.unmerged (the fold reminds it,
        then tells whoever raised it). A merged result is remembered by the daemon (after a restart — one check again); a
        git error is a log line and a retry in MERGED_RETRY, without a fact. git runs in the background, as the cleanup of
        copies (git status of a big copy takes seconds, the tick would stand); the next tick writes the result."""
        if self.config.get("host") != "codex":   # a Claude hub: as in main — no copy has a base, nothing to look at
            return
        alive = self.merged_worker is not None and self.merged_worker.is_alive()   # before taking: it has handed its own in
        with self.merged_lock:
            done, self.merged_done = self.merged_done, []
        now, seen = self.clock(), self.merged_seen
        for name, did, seq, info, (ahead, dirty, err) in done:
            if err:
                if (seen.get(name) or (None,))[0] != seq:   # a log line once per result, not on every retry
                    print(f"hubd: cannot check whether {name} ({info.get('branch')} → {info.get('base')}) is merged: {err}",
                          flush=True)
                seen[name] = (seq, now + MERGED_RETRY)
                continue
            seen[name] = (seq, None)
            if (ahead or dirty) and name in self.state["sessions"]:   # gone — a line to whoever raised it (on_unmerged)
                self.record("executor.unmerged", name, {"branch": info.get("branch"), "base": info.get("base"),
                                                        "ahead": ahead, "dirty": dirty, "seq": seq, "task": did,
                                                        "channel": "core", "owner": False})
        if alive:
            return
        for name in [n for n in seen if n not in self.state["sessions"]]:
            del seen[name]
        todo = [(name, did, seq, dict(info)) for name, did, seq, info in executors.unmerged_due(self.state)
                if not ((seen.get(name) or (None,))[0] == seq and (seen[name][1] is None or seen[name][1] > now))]
        if todo:
            self.merged_worker = threading.Thread(target=self._check_merged, args=(todo,), name="check-merged",
                                                  daemon=True)
            self.merged_worker.start()

    def _check_merged(self, todo):
        """The background of check_merged: only git and the list of results (under the lock)."""
        for name, did, seq, info in todo:
            if self.stopping:
                return
            try:
                got = worktree.merged(info)
            except Exception as e:   # noqa: BLE001 — the background does not fail silently: a result with a reason, a retry later
                got = (None, None, f"{type(e).__name__}: {e}")
            with self.merged_lock:
                self.merged_done.append((name, did, seq, info, got))

    def retire_executors(self):
        """An executor did its part and reported — put it to sleep without handing over its work; a handed-in one — remove after the term (core/executors.py)."""
        hours = (self.config.get("executors") or {}).get("remove_after_hours", executors.EXECUTOR_KEEP / 3600)
        for what, name in executors.retiring(self.state, self.clock(), keep=float(hours or 0) * 3600):
            if self.stopping:
                return   # putting to sleep waits up to 20 s for the session to exit: the rest wait for the next run
            if what == "sleep":
                self.record("session.close_requested", name, {"retire": True, **self.handed_cards(name)})
            else:
                self.record("session.remove_requested", name, {"retired": True})

    def handed_cards(self, name):
        """Board cards the handing-in executor worked on (core/board.py), with the status from disk: the reducer
        appends to the lead a line "handed in — close or note" (spec dir-sync K4). No link or an unreadable board — {}."""
        try:
            slug = board.direction_of(self.state, self.state["sessions"].get(name))
            mine = [c for s, c, _how in board.links(self.state, name) if s == slug]
            if not slug or not mine:
                return {}
            from modules.directions import direction
            have = {board.card_id(c["id"]) or c["id"]: c["status"]
                    for c in direction.cards(direction.path_of(slug, direction.root(state=self.state_dir)))}
            found = [{"slug": slug, "card": c, "status": have[c]} for c in mine if c in have]
            return {"cards": found} if found else {}
        except Exception as e:  # noqa: BLE001 — the card line must not prevent putting the finished executor to sleep
            print(f"hubd: cards {name}: {e!r}", flush=True)
            return {}

    def executor_base(self, run, raiser, name):
        """Where an executor gets its working copy: the task's repo, the spawner's folder, else its own folder (core/workspace.py)."""
        return run.get("repo") or raiser.get("cwd") or workspace.path(self.state_dir, name)

    ADMIT_POINT = "task.admit"
    ADMIT_PASS = 5.0   # seconds: asking stops after this long in one pass, the rest is asked on the next tick
    ADMIT_EVERY = 2.0  # seconds: a held request is asked again no more often than this (a tick is 0.3 s)

    def admit_pass(self, held):
        """Admission of executors' launches (Module API 2.10, the task.admit point), before a copy is made. Two calls in
        run_tasks: held=True asks the requests a module holds — to lift the hold (task.run_held {release}: the request
        waits for room or rises as any other) or to update its reason; held=False asks the ready ones whose copy is not
        yet being made, in load order of the modules that declared the point. {hold: reason} from one of them writes
        task.run_held {module, reason}: the request goes back to the queue with the reason as its why. A module that is
        silent, failed, answered wrongly, is not ready or was removed never holds (a hold it had is lifted); the point's
        budget bounds a miss, a module that missed is not asked again in the same pass, and a pass stops asking after
        ADMIT_PASS seconds — a ready request not asked yet waits for the next tick (self.admit_wait), nothing is raised
        unasked. Wakes (a message to a sleeping executor) are not launches and are not asked; a request waiting for room
        is asked when it becomes ready. A fact is written only on a change."""
        runs = self.state.get("runs") or {}
        if not runs:
            return
        try:
            names = list(self.modules.mods.answering(self.ADMIT_POINT))
        except Exception as e:  # noqa: BLE001 — a module's records must not hold the queue
            print(f"hubd: {self.ADMIT_POINT} not asked: {e!r}", flush=True)
            names = []
        if not names and not held:
            return   # no module asks: the common case costs nothing
        asked = sorted(((n, r) for n, r in runs.items() if not r.get("wake") and bool(r.get("held")) == held
                        and (held or (r.get("state", "ready") == "ready" and n not in self.preparing
                                      and n not in self.admitted))), key=lambda kv: kv[1].get("ts") or 0)
        now = time.monotonic()
        missed, until = set(), now + self.ADMIT_PASS
        for name, run in asked:
            if self.stopping:
                return
            if held and now < self.admit_next.get(name, 0):
                continue   # asked a moment ago
            if time.monotonic() > until:
                if not held:
                    self.admit_wait.add(name)   # not asked yet: not raised in this tick
                continue
            verdict = None
            for module in [m for m in names if m not in missed]:
                try:
                    got = self.modules.mods.call(module, self.ADMIT_POINT, {"run": self.admit_view(name, run)})
                except Exception as e:  # noqa: BLE001 — a crash of the call never holds
                    print(f"hubd: {self.ADMIT_POINT} {module}: {e!r}", flush=True)
                    got = None
                if got is None:
                    missed.add(module)
                elif got.get("hold"):
                    verdict = (module[:64], got["hold"])   # 64: as the fact keeps it (reducer, task.run_held)
                    break
            old = run.get("held")
            if verdict:
                self.admitted.discard(name)
            if verdict and (not old or (old["module"], old["reason"]) != verdict):
                self.record("task.run_held", name, {"module": verdict[0], "reason": verdict[1], "channel": "core"})
            elif not verdict and old:
                self.record("task.run_held", name, {"release": True, "channel": "core"})
            if verdict:
                self.admit_next[name] = time.monotonic() + self.ADMIT_EVERY
            else:
                self.admit_next.pop(name, None)
                if not missed:
                    self.admitted.add(name)   # every module answered {}: not asked again before its copy is made

    def admit_view(self, name, run):
        """What a module is told about a request: who asks, the task, where it runs — not the owner's word or the tools."""
        return {"name": name, "by": run.get("by"), "task": str(run.get("task") or "")[:4000], "repo": run.get("repo"),
                "branch": run.get("branch"), "here": bool(run.get("here")), "work": run.get("work"),
                "ts": run.get("ts"), "held": bool(run.get("held"))}

    def run_tasks(self):
        """An executor for a task (core/executors.py): its own working copy (branch task/<name>) and a spawn; on failure —
        task.run_failed, the reason goes to whoever spawned it."""
        self.admit_wait.clear()
        self.admit_pass(held=True)   # modules with the task.admit point: lift or keep the holds (nothing raised before it)
        # queue: a slot freed up or the weekly window reset — start spawning (10-04)
        for name in executors.promotable(self.state, self.clock()):
            self.record("task.run_ready", name, {"channel": "core", "owner": False})
        self.admit_pass(held=False)   # and ask the ready ones, those just promoted too
        runs, until = self.state.get("runs") or {}, time.monotonic() + self.PREPARE_WAIT   # waiting for copies — for the whole tick
        self.admitted &= set(runs)   # the let-through of a request that is gone is forgotten
        self.admit_next = {n: t for n, t in self.admit_next.items() if n in runs}
        for name in [n for n, job in self.preparing.items() if n not in runs and job["done"].is_set()]:
            del self.preparing[name]   # the task was withdrawn while the copy was being checked out: the copy stays under its name
        for name, run in list(runs.items()):
            if self.stopping:
                return   # a working copy is git, up to 30 s per call: the rest wait for the next run
            if run.get("state", "ready") != "ready" or name in self.admit_wait:
                continue   # waits in the queue: for a slot, the budget or the owner's word; or is not asked yet (admit_pass)
            raiser = self.state["sessions"].get(run.get("by") or "") or {}
            base = self.executor_base(run, raiser, name)
            if self.busy_copy(name, run, base):
                continue   # its copy is being cleaned by the background now — next tick
            if self.forwarding_into(base):
                continue   # git is fast-forwarding the folder it starts from (forward_folders) — next tick
            moved = None
            try:
                if run.get("here"):   # right inside a ready copy: we do not make our own (mini-hubs, slice 3)
                    (cwd, err), info = worktree.root(base), None
                    err = err or executors.here_refused(self.state, cwd, run.get("by"), name, inside=in_tree)
                else:
                    job, key = self.preparing.get(name), (run.get("ts"), run.get("repo"), run.get("branch"))
                    if job and job["key"] != key:   # a checkout for a withdrawn task under the same name — not its result
                        if not job["done"].is_set():
                            continue   # the same copy path: the new git goes after the old one
                        del self.preparing[name]
                        job = None
                    if job is None:
                        err, moved = self.free_branch(run, raiser, base) if run.get("branch") else (None, None)
                        job = None if err else self.start_prepare(name, base, run.get("branch"), moved, key,
                                                                  from_main=bool(run.get("from_main")))
                    if job is None:
                        cwd, info = None, None
                    elif not job["done"].wait(0 if job["waited"] else max(0, until - time.monotonic())):
                        job["waited"] = True
                        continue   # git is still checking out the copy — the next tick will take the result
                    else:
                        del self.preparing[name]
                        (cwd, info, err), moved = job["result"], job["moved"]
                    if run.get("branch") and not cwd and not err:
                        err = coretext.T("core.daemon.not_git", "{base} is not git — an executor on branch {branch} needs a "
                                          "folder with the repo: --repo <path>").format(base=base, branch=run["branch"])
                    if moved and info:
                        info["checks"].append([moved, True])
            except (OSError, subprocess.SubprocessError) as e:
                cwd, info, err = None, None, repr(e)[:300]
            if err:   # the mini-hub's folder was already switched but the copy failed — report that too
                # the reason with the marks of its phrases (reason_l10n): the message about it reads in the owner's language
                self.record("task.run_failed", name,
                            coretext.put({}, "reason", coretext.join(err, f" ({moved})") if moved else err))
                continue
            have = catalog.repo_tools(run.get("tools") or [], self.tools or self.state["catalog"], self.config,
                                      cwd or base, raiser.get("project") or "all")   # the folder's .mcp.json: no card for those
            for type, entity, data in executors.events_for(self.state, name, run, cwd=cwd or base,
                                                           worktree=info if cwd else None, here=bool(run.get("here")),
                                                           repo_tools=have):
                self.record(type, entity, data)
                if type == "session.requested" and \
                        (self.state["sessions"].get(name) or {}).get("session_id") != data["session_id"]:
                    # a rejection (the name was taken while the request waited) — withdraw the request, do not assign the task to another session
                    # (10-05 analysis: otherwise a new task for that session every 0.3 s)
                    self.record("task.run_failed", name, coretext.put({}, "reason", coretext.T(
                        "core.daemon.name_taken", "name {name} is taken by another session").format(name=name)))
                    break

    # how long a tick waits for the executors' working copies (all together): a small repo — spawn in the same tick, a big one — git
    # continues in the background
    PREPARE_WAIT = 2

    def start_prepare(self, name, base, branch, moved, key=None, from_main=False):
        """The executor's working copy — in the background (hosts/worktree.prepare): `git worktree add` of a big repo on a slow
        disk takes minutes, the tick would stand still (10-21: a 30 s timeout in the tick — git killed, the executor did not spawn). The background
        does not touch the state: run_tasks takes the result."""
        job = {"done": threading.Event(), "result": None, "moved": moved, "waited": False, "key": key}
        codex = self.config.get("host") == "codex"
        # several hubs on one machine (machine_hubs): an instance's namespace; with other hubs here — the owner mark,
        # another hub's copy refused, and the repo's file lock across the hubs
        machine = self.machine_hubs()
        hubs = {"space": machine["space"]} if machine["space"] else {}
        if machine["owner"]:
            hubs.update(owner=machine["owner"], foreign=lambda path: self.foreign_in(machine, path))

        def work():
            try:
                # two `worktree add` of one repo at once — git reads a neighbour's half-written entry (commondir): one at a time
                common = worktree.common_dir(base)
                repo = common or base
                with self.repo_locks_lock:
                    one = self.repo_locks.setdefault(repo, threading.Lock())
                with one, worktree.repo_lock(worktree.lock_target(base, machine["owner"], common), worktree.COPY_TIMEOUT):
                    job["result"] = worktree.prepare(base, name, branch=branch, **({"from_main": True} if from_main else {}),
                                                     # experiment, slice C: the copy's base — only on a hub on Codex
                                                     **({"with_base": True} if codex else {}), **hubs)
            except TimeoutError:
                job["result"] = (None, None, worktree.LOCK_BUSY.format(seconds=worktree.COPY_TIMEOUT))
            except Exception as e:   # noqa: BLE001 — the background is not silent: whoever spawned it gets the reason
                job["result"] = (None, None, repr(e)[:300])
            job["done"].set()
        job["thread"] = threading.Thread(target=work, name=f"worktree-{name}", daemon=True)
        self.preparing[name] = job
        job["thread"].start()
        return job

    def free_branch(self, run, raiser, base):
        """`--branch`: a branch open in a folder — free it for the executor -> (rejection, what was done). Open in a clean
        folder of the mini-hub that is spawning, and not shared with other sessions — switch the folder to the main branch (spec §7); with another
        executor or in someone else's folder — a rejection with the name: two on one branch would get in each other's way."""
        branch, main = run["branch"], worktree.main_of(base)
        if branch == main:
            return coretext.T("core.daemon.main_branch", "{branch} is the repo's main branch: an executor on it — without "
                             "--branch (a branch of its own from it)").format(branch=branch), None
        where = worktree.opened(base, branch)
        if not where:
            return None, None
        live = {n: s for n, s in self.state["sessions"].items() if s["state"] not in minihub.GONE and s.get("cwd")}
        mine = raiser.get("cwd") and in_tree(raiser["cwd"], where)
        if mine and minihub.on(self.state, raiser):
            others = sorted(n for n, s in live.items() if n != raiser["name"] and in_tree(s["cwd"], where))
            other_hub = self.foreign_in(self.machine_hubs(), where)   # several hubs: a session of another hub there
            if other_hub:
                others.append(other_hub)
            if others:
                return coretext.T("core.daemon.branch_shared", "branch {branch} is open in your folder {where}, "
                                  "{others} share it too — I will not switch the branch under them: an executor without "
                                  "--branch, or switch the folder yourself").format(branch=branch, where=where,
                                                                                   others=", ".join(others)), None
            main, err = worktree.to_main(where, branch, main)
            return err, None if err else coretext.T("core.daemon.switched", "folder {where} was switched from {branch} to "
                                                    "{main}").format(where=where, branch=branch, main=main)
        if mine:
            return coretext.T("core.daemon.branch_mine", "branch {branch} is open in your folder {where}: switch the folder "
                             "to another branch, or without --branch").format(branch=branch, where=where), None
        who = next((n for n, s in live.items() if worktree.same((s.get("worktree") or {}).get("path") or "", where)
                    or in_tree(s["cwd"], where)), None)
        if not who:
            return coretext.T("core.daemon.branch_open", "branch {branch} is already open in {where} — two on one branch "
                             "would get in each other's way").format(branch=branch, where=where), None
        hint = "; " + coretext.T("core.daemon.branch_continue", "to continue in its copy — hub task run … --repo {where} "
                                 "--here").format(where=where) if executors.dormant(live[who]) else ""
        return coretext.T("core.daemon.branch_taken", "branch {branch} is already open at {who} ({where}) — two on one "
                          "branch would get in each other's way").format(branch=branch, who=who, where=where) + hint, None

    def restart(self):
        """`hub restart`: it is time (sessions are free, no background work, the waiting ceiling ran out — or parts were requested) — a file for
        hub-up, which does the restart."""
        now, minutes = self.clock(), restart.ceiling(self.config)
        want = restart.due(self.state, now, minutes)
        line = restart.page_note(self.state, now, minutes)
        if line != getattr(self, "page_hold_said", None):   # to the log — on a change, not every tick
            self.page_hold_said = line
            print(f"hubd: {line or 'the restart no longer waits for the page'}", flush=True)
        if want is None:
            return
        path = os.path.join(self.state_dir, restart.FLAG)
        try:   # hub-up has not yet taken the previous request: overwriting it would lose it (the whole hub after an update) — this one waits
            if abs(time.time() - os.path.getmtime(path)) < restart.UNREAD:   # the clock stepped back — also fresh
                return
        except OSError:
            pass
        self.record("hub.restart_started", data=want)
        with open(path + ".tmp", "w") as f:
            json.dump({"parts": want["parts"]}, f)
        platforms.current.replace(path + ".tmp", path)

    def ingest(self):
        """Only the inbox — at start, while the host has no processes; in a tick — collect."""
        for path, item in spool.take(self.spool_dir):
            self.spooled(path, item)

    def spooled(self, path, item):
        """An inbox fact goes to the journal, then the file leaves the inbox: if the write fails the file stays and comes next time.
        core.* events (snapshot, rejection, start, cleanup) are written by the core only: journal readers replace the whole state at a snapshot
        and count the previous event as rejected at a rejection — such a fact does not go into the journal even as rejected.
        In its place goes a spool.refused stub with no data (only who asked, for the CLI) — the fold rejects it; the same for a
        module, core.write included. A module fact (the module field): with core.write — with the by label and src mod:<name>, a forbidden one is a
        rejection (core/corewrite.py). A file of the wrong shape (spool.malformed) goes to .bad, unrecorded: the queue moves on."""
        if path in self.taken:
            self.drop(path)
            return
        why = spool.malformed(item)
        if why:
            spool.set_aside(path, why)
            return
        if req_key(item) in self.recorded_reqs:
            self.drop(path)   # recorded before the previous hubd died, the file stayed — do not write it a second time
            return
        d = item.get("data") if isinstance(item.get("data"), dict) else {}
        if item.get("src") == "hook" and item.get("type") == "turn.started" \
                and self.taken_by_host.pop((item.get("entity"), d.get("marker")), None):
            self.drop(path)   # a cut hook that outlived its timeout: the host has already put this start (collect)
            return
        kind, data = item.get("type"), item.get("data")
        # and the mailbox check's own count and mode (core/admit.py): the fold takes them, and a file could switch the mode
        # and the accepted state of the watched settings (core/configwatch.py): a file could clear a row or move the state
        refused = isinstance(kind, str) and (kind.startswith("core.") or kind in admit.OWN or kind in configwatch.FACTS
                                       or kind in appwatch.FACTS)
        forged = refused and not kind.startswith("core.")   # still checked and flagged (core_only): an attempt shows in the window and stats
        asked = kind
        by, deny = self.corewrite.admit(item) if "module" in item and not refused else (None, None)
        # module, plugin: the channel and the sender are set by the core; a module with core.write (the owner's word) writes core events itself
        stamped, alien = spool.outsider(item) if by is None else (None, None)
        if stamped is not None:
            data, deny = stamped, deny or (None if refused else alien)   # core.* is rejected by the stub with its own reason
        if kind == "account.limits" and stamped is not None and not deny:
            # from a module — only the login module, its own profile; Codex's — only the Codex pool's module, its row
            deny = self.codex_pool.admits(data.get("sender"), {**data, "type": kind}, self.state) \
                if data.get("engine") == "codex" else self.profiles.admits(data.get("sender"), data)
        if kind == "codex.account_lost" and stamped is not None and not deny:
            deny = self.codex_pool.admits(data.get("sender"), {**data, "type": kind}, self.state)
        if kind in busstate.TRANSPORT_KINDS and stamped is not None and not deny:
            deny = self.bus_transport_admits(data.get("sender"))   # from a module — only with the bus.transport right
        if kind in executors.PAUSE_KINDS and stamped is not None and not deny:
            deny = self.queue_pause_admits(data.get("sender"))   # from a module — only with the queue.pause right
        if kind == "message.received" and stamped is not None and data.get("now") and not deny:
            deny = self.rush_admits(data.get("sender"))   # urgent from a module — only with the message.rush right
        if refused:
            d = data if isinstance(data, dict) else {}
            kind, data = "spool.refused", {"type": kind, **{k: d[k] for k in ("req", "sender", "channel", "owner")
                                                             if k in d}}
        src = f"mod:{by}" if by else item.get("src", "spool")
        # the repeat key (hubmod.fact) is an event field, not data: only for a module fact whom the core named mod:<name>
        sender = data.get("sender") if stamped is not None and not refused else None
        dedup = item.get("dedup") if isinstance(sender, str) and sender.startswith("mod:") else None
        checking = not (refused or by or deny) or forged
        flag = self.admit(asked, item.get("entity"), data, src) if checking else None
        if flag and admit.mode(self.state) == "refuse" and not forged:
            deny = flag   # refuse mode: the fact and its core.rejected — the fold does not apply it, the CLI prints why
        ev = self.record(kind, item.get("entity"), data, src=src, by=by, deny=deny, dedup=dedup)
        if checking:   # checked: counted (the time is the fact's write time: no extra call to the core clock)
            self.checked = [self.checked[0] + 1, ev["ts"] if self.checked[1] is None else self.checked[1]]
        if flag:   # both modes (core/admit.py): next to the fact — why it fails the check (statistics count it)
            self.record("spool.flagged", None, admit.flagged(ev["seq"], asked, src, data or {}, flag))
            self.count_checked(force=True)   # and the count with it: a flag does not outrun its denominator (stats windows)
        self.drop(path)
        if item.get("type") == "account.limits":   # from the account gateway — as from the host: the switch before the results of the same poll
            self.switch_accounts()

    def bus_transport_admits(self, sender):
        """bus.offered / bus.handed from a module process (sender mod:<name>, named by the core): only a module hubd
        runs with the Module API right bus.transport. None — accept; otherwise the refusal reason."""
        name = sender[4:] if isinstance(sender, str) and sender.startswith("mod:") else None
        w = (self.modules.written or {}).get(name) if name else None
        if not isinstance(w, dict) or busstate.TRANSPORT_RIGHT not in (w.get("capabilities") or []):
            return f"bus envelopes from {sender or 'a module'}: only a module with the {busstate.TRANSPORT_RIGHT} right"
        return None

    def queue_pause_admits(self, sender):
        """task.queue_paused / task.queue_resumed from a module process (sender mod:<name>, named by the core): only a
        module hubd runs with the Module API right queue.pause. Any other outsider is left to the fold (the hub or the
        owner pauses). None — accept; otherwise the refusal reason."""
        name = sender[4:] if isinstance(sender, str) and sender.startswith("mod:") else None
        if name is None:
            return None
        w = (self.modules.written or {}).get(name)
        if not isinstance(w, dict) or executors.PAUSE_RIGHT not in (w.get("capabilities") or []):
            return f"the queue pause from {sender}: only a module with the {executors.PAUSE_RIGHT} right"
        return None

    def rush_admits(self, sender):
        """An urgent message (now) from a module process (sender mod:<name>, named by the core): only a module hubd
        runs with the Module API right message.rush. Other outsiders (a plugin) are left to the fold, which refuses
        them. None — accept; otherwise the refusal reason."""
        name = sender[4:] if isinstance(sender, str) and sender.startswith("mod:") else None
        if name is None:
            return None
        w = (self.modules.written or {}).get(name)
        if not isinstance(w, dict) or reducer.RUSH_RIGHT not in (w.get("capabilities") or []):
            return f"urgent (now) from {sender}: only a module with the {reducer.RUSH_RIGHT} right"
        return None

    def admit(self, kind, entity, data, src):
        """Check an inbox fact (core/admit.py) against the state before it: a reason or None. A check failure does not crash the core —
        the fact is written as without it. The number checked goes as a spool.checked fact (count_checked): hub stats divide the flags
        by it."""
        try:
            return admit.check(self.state, kind, entity, data, src)
        except Exception as e:  # noqa: BLE001
            print(f"hubd: mailbox check — {e.__class__.__name__}: {e}", flush=True)
            return None

    def count_checked(self, force=False):
        """How many inbox facts were checked — into the journal no more than once per CHECKED_EVERY, right after a flag and when the core stops
        (a clock stepped back — also written: otherwise we would wait until it catches up)."""
        n, since = self.checked
        if n and (force or not 0 <= self.clock() - since < CHECKED_EVERY):
            self.write_checked()
            self.settle_admit()   # a new day in the window may complete it

    def write_checked(self):
        n, since = self.checked
        if n:
            self.checked = [0, None]
            self.record("spool.checked", None, {"n": n, "since": round(since, 3), "mode": admit.mode(self.state),
                                                "set": admit.setting(self.config)})

    def settle_admit(self):
        """The mailbox check mode (core/admit.py) per hub.json admit.mode and the hub's own window; a change of the mode or of
        the setting is the core fact spool.mode (before the first one: flag, auto), the count so far goes before it
        under the old mode. Unreadable hub.json — the mode stays: the core keeps the previous settings, and on defaults an
        owner's explicit flag would be lost."""
        if not self.settings_known:
            return
        now, setting = self.clock(), admit.setting(self.config)
        to = admit.want(setting, self.state, now)
        if (to, setting) == (admit.mode(self.state), (self.state.get("admit") or {}).get("set") or "auto"):
            return
        self.write_checked()
        seen, flags = admit.window(self.state, now)
        self.record("spool.mode", None, {"mode": to, "set": setting, "days": seen, "flags": flags})

    def drop(self, path):
        """Delete an inbox file whose fact is already in the journal; if it cannot be deleted (Windows: it is held) — remember it so as not to take it
        a second time."""
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError:
            self.taken.add(path)
            return
        self.taken.discard(path)

    def collect(self):
        """Hooks (the inbox) and session output (the host) go into the journal in the order they happened (10-13: "the whole
        inbox first, then the whole host" put the result of turn A after the start of turn B). The host — up to the poll moment P, lines with the read
        time; the inbox — by name, while the fact's time is not later than P: the first one later than P and all after it wait for the next tick (the files
        wait in the inbox). Each source keeps its own order, time only decides whose fact is next; on a tie — the inbox
        first (a hook blocks Claude Code: its line after the hook is later). A time later than P (the clock stepped back: for the inbox —
        later than "now", for the host — later than the poll) counts as P. The journal time is the write time, not the event's: the core
        counts deadlines from it (input confirmation etc.), and an event time from the past would shorten them."""
        stamped = getattr(self.host, "stamped", None)
        if stamped:
            until, lines = stamped()
        else:   # a host without time (tmux, fakes): its facts go after the inbox, as before
            until = self.clock()
            lines = [(until, *f) for f in self.host.poll()]
        files = spool.take(self.spool_dir)
        now = self.clock()   # after looking at the inbox: a file that landed meanwhile is not "from the future"
        hooks = []
        for path, item in files:
            ts = item.get("ts") if isinstance(item, dict) else None   # of the wrong shape — spooled will set it aside
            at = ts if isinstance(ts, (int, float)) else float("-inf")
            if until < at <= now:
                break
            hooks.append((min(at, until), path, item))
        lines = [(min(at, until), *f) for at, *f in lines]
        i = j = 0
        while i < len(hooks) or j < len(lines):
            if j == len(lines) or i < len(hooks) and hooks[i][0] <= lines[j][0]:
                self.spooled(*hooks[i][1:])
                i += 1
                continue
            _, type, name, data = lines[j]
            j += 1
            if data.get("fallback") == "hook":   # the fact of a core hook Claude Code cut (hosts/streamjson HookWatch)
                if not reducer.hook_lost(self.state, type, name, data):
                    continue   # the hook wrote it all the same: it is folded above
                if type == "turn.started":   # a message is taken once: its mark names this start alone
                    self.taken_by_host[(name, data.get("marker"))] = True
                    while len(self.taken_by_host) > 200:
                        self.taken_by_host.pop(next(iter(self.taken_by_host)))
            self.record(type, name, self.hold_input(name, data) if type == "permission.requested" else data, src="host")
            if type == "account.limits":
                self.switch_accounts()

    def hold_input(self, name, data):
        """A permission request for the journal: known key shapes in the tool input are masked (this is how the page, Telegram and archives
        see it), the input as is stays here, for the reply. A question to the owner stays as written: answers
        are matched by the question text. The input of requests no longer in the state (the process left) is forgotten here too."""
        held = {(n, r["request_id"]) for n, s in self.state["sessions"].items() for r in s.get("pending") or []}
        self.inputs = {k: v for k, v in self.inputs.items() if k in held}
        raw = data.get("input")
        if data.get("tool") == "AskUserQuestion" or not isinstance(raw, dict):
            return data
        masked = redact.mask_data(raw)
        if masked == raw:
            return data
        self.inputs[(name, data.get("request_id"))] = raw
        return {**data, "input": masked, "masked": True}

    def hold_pending(self):
        """The input of waiting requests recorded before masking (an earlier hub) goes to memory: the state scrub will mask it."""
        for name, sess in self.state["sessions"].items():
            for req in sess.get("pending") or []:
                raw = req.get("input")
                if req.get("tool") != "AskUserQuestion" and isinstance(raw, dict) and redact.mask_data(raw) != raw:
                    self.inputs[(name, req["request_id"])] = raw

    def give_input(self, name, rid, response):
        """A reply to a masked request with the input as the session sent it: what the core added (answers) or changed
        on top of the masked copy stays. No input (hubd was restarted — the request would have ended with the session process) —
        a rejection, never a tool on [masked]."""
        raw = self.inputs.pop((name, rid), None)
        if response.get("behavior") != "allow":
            return response
        if raw is None:
            return {"behavior": "deny", "message": "The hub no longer has the input of this request (the core was "
                                                   "restarted): ask again."}
        masked, sent = redact.mask_data(raw), dict(response.get("updatedInput") or {})
        return {**response, "updatedInput": {**raw, **{k: v for k, v in sent.items() if masked.get(k) != v}}}

    def providers_tick(self):
        """Who runs session login, the voice, the report (core/providers.Tick): builtin -> shadow -> module and back. An automaton error
        does not crash the tick — everything stays as it was (the built-in one, if the module was not in charge)."""
        try:
            self.providers.tick()
        except Exception as e:  # noqa: BLE001
            print(f"hubd: feature providers — {type(e).__name__}: {e}", flush=True)

    def switch_accounts(self):
        """Session login (core/profiles.py: the pool in the core tree or the login module): the pool appeared, emptied, is paused,
        the active one hit its limit, the first one reset — a fact into the journal; the fold switches. First come the token fingerprints
        (account.token): a new token of the same account before the switch decision. Every tick and right after limits from the host
        — before the turn results of the same poll. The module did not answer — nothing is written."""
        self.profiles.decide(lambda: self.state, self.switch_record)

    def switch_record(self, kind, data):
        """A pool decision into the journal; a move — with its cost to the fleet in the log: every live session writes its
        whole conversation into the prompt cache again on the other login (10-27: three moves in an hour, about $65 each).
        An estimate — live sessions and the sum of their last context in tokens (the price depends on the model)."""
        # the reason with the marks of its phrases (why_l10n): the note to the hub reads in the owner's language
        self.record(kind, None, {**(coretext.put(dict(data), "why", data["why"]) if data.get("why") else data),
                                 "channel": "core"})
        if kind == "account.switched" and not data.get("free") \
                and (data.get("to") != data.get("from") or data.get("refresh") or data.get("fresh")):
            live = [s for s in self.state["sessions"].values() if s["state"] in LIVE]
            tokens = sum(s["context"] for s in live if isinstance(s.get("context"), int))
            was, to = data.get("from") or "the default login", data.get("to") or "the default login"
            print(f"hubd: Claude account {was} → {to}: prompt cache rebuilt for "
                  f"{len(live)} live sessions, ~{round(tokens / 1000)}k tokens", flush=True)

    CODEX_TELL_AFTER = 120   # s; experiment: how long "Codex does not let in" holds before the owner is told

    def codex_mark(self):
        """Codex (experimental): the fold knows codex/… models only on a hub with the Codex host (the mark codex.host); the mark off
        also drops a sign-in loss left from the Codex time (a core before this wrote the mark and kept the loss). Also at
        the start, before the mailbox: a fresh hub must not refuse codex/… left there before its first tick. whole — every
        session is on Codex (the host's all_sessions: no claude limits come): the hub's limits row is Codex's
        (reducer._codex_limits). -> on."""
        on = getattr(type(self.host), "codex_models", False) is True   # made by make_host: "host": "codex" or HUB_CODEX=1
        if not on and not self.state.get("codex"):
            return False   # a Claude hub that never had Codex: as in main
        whole = on and getattr(self.host, "all_sessions", False) is True
        cx = self.state.get("codex") or {}
        if on != catalog.codex_state(self.state) or (not on and cx.get("lost")) or whole != bool(cx.get("whole")):
            self.record("codex.host", None, {"on": on, **({"whole": True} if whole else {}), "channel": "core",
                                             "owner": False})
        return on

    def codex_tick(self):
        """Codex (experimental): the Codex limit window has reset — continue the turns it cut
        (reducer.codex_due -> codex.limit_reset; one fact per tick, the fold continues)."""
        on = self.codex_mark()
        if not on and not self.state.get("codex"):
            return   # a Claude hub that never had Codex: as in main
        if not on:   # rolled back to claude (stand 2026-11-05): no Codex steps; a notice "does not let in" left open goes
            if f"n:core:{reducer.CODEX_NOTICE}" in (self.state.get("notices") or {}):
                self.record("owner.notify_resolved", None, {"id": reducer.CODEX_NOTICE, "channel": "core", "owner": False})
            self.codex_resolve(set())   # and the accounts' ones
            self.codex_active_file()     # the core's model back in the hub folder
            return
        if reducer.codex_due(self.state, self.clock()):
            self.record("codex.limit_reset", None, {"channel": "core"})
        self.codex_pool_tick()
        # no sign-in: only the owner is told (the page's notices, Telegram) — not the hub session, which cannot work
        # without it either; once per loss (lost.told is in the state: closed or restarted — not again), after it has
        # held CODEX_TELL_AFTER (a one-off 401 that the next good turn clears is not told); a sign-in clears the notice
        lost = (self.state.get("codex") or {}).get("lost")
        shown = f"n:core:{reducer.CODEX_NOTICE}" in (self.state.get("notices") or {})
        if lost and not lost.get("told") and self.clock() - (lost.get("since") or lost.get("ts") or 0) >= self.CODEX_TELL_AFTER:
            self.record("owner.notify", None, {"id": reducer.CODEX_NOTICE, "title": "Codex",
                                               "text": reducer.codex_lost_text(self.state), "channel": "core",
                                               "owner": False})
        elif not lost and shown:
            self.record("owner.notify_resolved", None, {"id": reducer.CODEX_NOTICE, "channel": "core", "owner": False})

    def codex_pool_tick(self):
        """Codex (experimental): the Codex account pool (core/codexslot.py) — its rows and the module's decision into the journal; an
        account's lost sign-in is told to the owner once (after CODEX_TELL_AFTER, as the hub's), a new sign-in clears it."""
        cx = self.state.get("codex") or {}
        logins = [cx.get("login"), (cx.get("account") or {}).get("logins")]
        if logins != self.codex_logins:
            self.codex_logins = logins
            self.codex_pool.fresh()   # a sign-in or a sign-out: the module's rows change at once (which have auth.json)
        rows = ((self.state.get("codex") or {}).get("pool") or {}).get("profiles")
        self.codex_pool.tick(lambda: self.state, self.codex_record)
        self.codex_active_file()
        new = ((self.state.get("codex") or {}).get("pool") or {}).get("profiles")
        # from the host's class, as wait_codex_folder: a host that answers every name prepares nothing
        if (new != rows or not getattr(self, "codex_rows_seen", False)) and \
                callable(getattr(type(self.host), "prepare_codex", None)):
            # the rows the core took (and those of the state at hubd's start): their folders prepared in the host's
            # background now, not on the launches after a switch; the tick does not wait for it
            self.codex_rows_seen = True
            for row in new or []:
                if not row.get("hub"):
                    self.host.prepare_codex(row["home"])
        lost = ((self.state.get("codex") or {}).get("account") or {}).get("lost") or {}
        now = self.clock()
        # every row lost: the hub's own "does not let in" says it (codex_tick) — not one more notice per account
        whole = bool((self.state.get("codex") or {}).get("lost"))
        for name, l in sorted(lost.items()):
            if not whole and not l.get("told") and now - (l.get("since") or l.get("ts") or 0) >= self.CODEX_TELL_AFTER:
                self.record("owner.notify", None, {"id": reducer.codex_notice(name), "title": "Codex",
                                                   "text": reducer.codex_account_lost_text(self.state, name),
                                                   "channel": "core", "owner": False})
        self.codex_resolve(set(lost) if not whole else set())

    def codex_active_file(self):
        """<state>/codex-active.json — the Codex pool's active account and folder for processes without the state (the
        core's model: core/codexcli.active_home); written on a change."""
        from core import codexcli
        cx = self.state.get("codex") or {}
        want = {"account": (cx.get("account") or {}).get("active"), "home": reducer.codex_active_home(self.state)}
        if want == getattr(self, "codex_active", None):
            return
        path = os.path.join(self.state_dir, codexcli.ACTIVE)
        try:
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(want, f)
            os.replace(path + ".tmp", path)
            self.codex_active = want
        except OSError as e:
            print(f"hubd: {codexcli.ACTIVE} not written — {e}", flush=True)

    def codex_resolve(self, keep):
        """The owner's notices of Codex accounts' lost sign-ins whose account is not in keep go."""
        head = f"n:core:{reducer.CODEX_NOTICE}:"
        for key in sorted(self.state.get("notices") or {}):
            if key.startswith(head) and key[len(head):] not in keep:
                self.record("owner.notify_resolved", None, {"id": reducer.codex_notice(key[len(head):]),
                                                            "channel": "core", "owner": False})

    def codex_record(self, kind, data):
        """A Codex pool decision into the journal; a move — with its cost in the log, as switch_record for Claude."""
        self.record(kind, None, {**data, "channel": "core"})
        if kind == "codex.switched" and not data.get("free") and data.get("to") != data.get("from"):
            live = [s for s in self.state["sessions"].values() if s["state"] in LIVE]
            print(f"hubd: Codex account {data.get('from') or 'hub folder'} → {data.get('to') or 'hub folder'}: "
                  f"sessions switch on their next message ({len(live)} live)", flush=True)

    def gateway_tick(self):
        """The account gateway (modules/gateway, its own process): the effective "on" goes into the journal (gateway.changed), the pool
        snapshot goes to the gateway (view.json). Never switched on — nothing: no files, no events. Session login belongs to the login module —
        the gateway is its too (route_tick). No built-in gateway in the tree (the open flavor, core/cutout.py) — only that."""
        if not cutout.present("gateway"):
            self.route_tick()
            return
        from modules.gateway import gateway as gw
        if not self.profiles.gateway():
            self.gateway_down = self.gateway_up = None   # the built-in pulse is not about the module's gateway
            self.gateway_fallback = False
            self.route_tick()
            if os.path.isdir(gw.folder(self.state_dir)):
                self.write_gateway_view()   # the built-in one finishes serving its sessions or yields the place to the module
            return
        known = self.state.get("gateway")
        cfg = gw.settings(self.config)
        if not cfg["enabled"] and not known:
            return
        now, status = self.clock(), gw.status_of(self.state_dir)
        probe = (status or {}).get("probe") or {}
        healthy = gw.alive(status, now)
        if healthy or not (known or {}).get("on"):
            self.gateway_down = None
        elif self.gateway_down is None:
            self.gateway_down = now
        # steady means a live pulse for STEADY in a row by the core clock (the gateway's since is the port opening: a frozen process with an
        # open port would pass the check with the very first pulse)
        self.gateway_up = (self.gateway_up or now) if healthy else None
        if self.gateway_fallback and healthy and now - self.gateway_up >= gw.STEADY:
            self.gateway_fallback = False
        why = None
        if not cfg["enabled"]:
            why = GATEWAY_OFF
        elif getattr(self.host, "kind", "stream") != "stream":
            why = GATEWAY_STREAM
        elif probe and not probe.get("ok"):
            why = coretext.T("core.daemon.gateway_probe", "the self-check failed: {why}").format(
                why=probe.get("why") or coretext.T("core.daemon.gateway_no_search", "no tool search"))
        elif self.gateway_down is not None and now - self.gateway_down >= gw.DOWN_WAIT:
            self.gateway_fallback, self.gateway_down = True, None
        if self.gateway_fallback and not why:
            # the text starts as forms.PREFIXES[NO_ANSWER]: a reason without its code is told by it
            why = forms.Why(coretext.T("core.daemon.gateway_down", "not answering for {seconds} s — sessions come up "
                                       "directly until it returns").format(seconds=gw.DOWN_WAIT), forms.NO_ANSWER)
        was = known or {}
        # it switches on when it serves; once on, it holds through a short gap (a crashed gateway restarting, the self-check)
        on = not why and (bool(was.get("on")) or healthy and bool(status.get("ident")))
        ident = (status["ident"] if healthy else was.get("ident")) if on else None
        failed = why if cfg["enabled"] and not on else None   # enabled but not working — tell the hub (once per reason)
        if (on, ident) != (bool(was.get("on")), was.get("ident")) or failed and failed != was.get("why"):
            self.gateway_changed(on, ident, why if failed else None, forms.code_of(why) if failed else None)
        self.write_gateway_view()

    def gateway_changed(self, on, ident, failed=None, code=None):
        """gateway.changed from the core: whether the gateway is on and its place; failed — why an enabled one is not working
        (with the marks of its phrases, why_l10n: the owner reads it in his language)."""
        self.record("gateway.changed", None, {"on": on, "ident": ident, "channel": "core",
                                              **({**coretext.put({}, "why", failed), "failed": True} if failed else {}),
                                              **({"code": code} if failed and code else {})})

    def route_tick(self):
        """The login module's gateway (the route of its profile.list, core/profiles.route): "on" or the place changed, a new failure
        reason or none any more — the core writes gateway.changed, as for the built-in one. The module did not answer — nothing
        changes (sessions on its gateway wait for the module process to restart); never switched on — no event. No login module
        and no built-in gateway in the tree (the open flavor): an "on" gateway is off — sessions on it come up directly."""
        known = self.state.get("gateway")
        if self.profiles.sign_in() is None and not cutout.present("gateway"):
            if (known or {}).get("on"):
                self.gateway_changed(False, None, NO_LOGIN_MODULE, forms.NO_ANSWER)
            return
        route = self.profiles.route(fresh=bool((known or {}).get("on")))
        if route is None:
            return
        self.route_seen = route   # whether the built-in one yields the place (write_gateway_view)
        if not route and not known:
            return
        why = route.get("why")
        if route.get("on") and getattr(self.host, "kind", "stream") != "stream":
            route, why = {"on": False}, GATEWAY_STREAM
        on = bool(route.get("on"))
        ident = route.get("ident") if on else None
        was = known or {}
        failed = why if not on and why else None
        # and the previous reason (the built-in one's, the module's) is cleared: otherwise the page and doctor would show someone else's failure
        if (on, ident) != (bool(was.get("on")), was.get("ident")) or failed != was.get("why") and (failed or not on):
            self.gateway_changed(on, ident, failed, route.get("code"))

    def write_gateway_view(self):
        """view.json for the gateway — on change: the active account, the busy ones, generations, how many live sessions are on the gateway. When session
        login belongs to the login module — for the built-in one: how many sessions are still on its place (to finish serving them) and whether to yield it (yield:
        the module's gateway takes the same place — port and secret)."""
        from modules.gateway import gateway as gw
        if self.profiles.gateway():
            users = sum(1 for s in self.state["sessions"].values() if s.get("gateway") and s["state"] in LIVE)
            extra = {}
        else:
            place = (gw.identity(self.state_dir) or {}).get("ident")
            users = sum(1 for s in self.state["sessions"].values() if place and s.get("gateway") == place
                        and s["state"] in LIVE)
            route = getattr(self, "route_seen", None) or {}
            extra = {"yield": True} if place and route.get("on") and route.get("ident") == place else {}
        # the claude the sessions spawn with: the gateway checks this very one (gateway_ready compares its key), not its own guess
        cmd = getattr(self.host, "claude", None)
        if isinstance(cmd, (list, tuple)):
            extra["claude"] = list(cmd)
        # the gateway is a companion of the built-in pool (core/profiles.gateway): its "on", not the login module's answer
        view = {**gw.core_view(self.state.get("account"), self.profiles.builtin.listing()["on"], users), **extra}
        if view != getattr(self, "gateway_view", None):
            gw.write(self.state_dir, "view.json", view)
            self.gateway_view = view

    def gateway_ready(self):
        """A spawn on the gateway: the gateway serves at the place the core knows and is checked on the current claude binary. None — go ahead;
        otherwise why we wait."""
        from modules.gateway import gateway as gw
        known = self.state.get("gateway") or {}
        status = gw.status_of(self.state_dir)
        if not gw.alive(status, self.clock()):
            return "the account gateway does not answer"
        if status.get("ident") != known.get("ident") or (gw.identity(self.state_dir) or {}).get("ident") != known.get("ident"):
            return "the account gateway moved"
        if ((status.get("probe") or {}).get("key") or "") != gw.claude_key(getattr(self.host, "claude", ["claude"])):
            return "the account gateway has not checked this claude yet"
        if not gw.reachable(status.get("port") or 0):
            return "the account gateway port does not answer"
        return None

    def budget_mode(self):
        """Limit-saving mode (core/budget.py) — only with mini-hubs: the pool reserve against thresholds; if it changed — a fact
        budget.mode (the fold does not depend on clocks and settings). No mini-hubs left — back to normal."""
        have = (self.state.get("budget") or {}).get("mode") or "normal"
        if budget.mode(self.state) is None:
            if have != "normal":
                self.record("budget.mode", None, coretext.put({"mode": "normal", "channel": "core"}, "why",
                                                              budget.WHY_NO_MINIHUBS))
            return
        (names, switch_at, renewable), now = self.profiles.names(), self.clock()   # the pool is session login (core/profiles)
        st = self.state
        if self.codex_whole():
            # Codex (experimental): a hub entirely on Codex saves by its Codex pool — the same rule on the Codex pool's record; its
            # module has not given its list yet (a hub restart): not known, the mode stays as it is
            if self.codex_pool.silent():
                return
            (names, switch_at, renewable) = self.codex_pool.names()
            st = {**st, "account": (st.get("codex") or {}).get("account") or {}}
        elif self.profiles.starting():   # the login module has not started yet: its pool is not known, keep the mode as is
            return
        cfg = (getattr(self, "config", None) or {}).get("budget")   # the saving threshold is budget.economy_at in hub.json
        want = budget.decide(st, names, switch_at, now,
                             cfg.get("economy_at") if isinstance(cfg, dict) else None, renewable)
        # the same mode — a fact only on a new window or reset time (the 5 h one reset, the weekly one holds): the reason and time on the
        # page and in the queue do not go stale, while the share, changing every turn, does not clutter the journal
        now_b = self.state.get("budget") or {}
        moved = abs((want.get("until") or 0) - (now_b.get("until") or 0)) > 60   # jitter of the reset stamp is not a change
        if want["mode"] == have and (have == "normal" or not moved and all(want.get(k) == now_b.get(k)
                                                                         for k in ("window", "account"))):
            return
        if want["mode"] != have and want["mode"] != "normal" and want.get("window"):   # the forecast is only for the login card
            want["eta"] = budget.forecast(self.state_dir, st.get("account"), want.get("account"),
                                          want["window"], now, (self.state.get("limits") or {}).get("engine"))
        # the reason is the core's phrase: its marks go with it (why_l10n), the owner reads it in his language
        self.record("budget.mode", None, coretext.put({**want, "channel": "core"}, "why", want.get("why")))

    def revive_asks(self):
        """A card gone stale with no answer: the fact ask.expired, the fold writes the session one message
        (core/askexpired.py). A few sessions per tick."""
        for name, ids in askexpired.due(self.state, self.clock()):
            if self.stopping:
                return
            self.record("ask.expired", name, {"channel": "core", "asks": ids})

    def resume_cut(self):
        """A turn cut by a limit on a hub without an account pool: its window reset - the fact session.limit_reset, the fold
        continues the session (core/limitresume.py; with a pool it is the accounts module's, modules/accounts). A few per
        pass, a pass once per limitresume.EVERY."""
        now = self.clock()
        if now < self.cut_next:
            return
        self.cut_next = now + limitresume.EVERY
        for name in limitresume.due(self.state, now):
            if self.stopping:
                return
            self.record("session.limit_reset", name,
                        {"channel": "core", "resets": self.state["sessions"][name]["limit_cut"]["resets"]})

    def observe(self):
        self.collect()
        if self.stopping:
            return
        gone = []
        delayed = self.waiting()
        for name, sess in list(self.state["sessions"].items()):
            if sess["state"] not in LIVE or name in delayed:
                continue
            info = self.host.info(name)
            if info is None:
                gone.append(name)
                continue
            reg = self.registry(info["pid"])
            seen = {k: v for k, v in (("pid", info["pid"]), ("pane", info.get("pane"))) if v is not None}
            if reg.get("messagingSocketPath"):
                seen["socket"] = reg["messagingSocketPath"]
            if any(sess.get(k) != v for k, v in seen.items()):
                self.record("session.identified", name, seen, src="observer")
            if not self.host.screen_dialogs:
                continue  # with stream-json, questions and permissions arrive as requests, not through the registry
            waiting = reg.get("status") == "waiting"
            state = self.state["sessions"][name]["state"]
            if waiting and state in ("idle", "busy"):
                self.record("dialog.opened", name, {"waitingFor": reg.get("waitingFor")}, src="registry")
            elif not waiting and state == "dialog":
                self.record("dialog.closed", name, {"status": reg.get("status")}, src="registry")
        if gone:
            self.collect()  # SessionEnd from /exit may have arrived between taking the inbox and the process dying
            for name in gone:
                if self.state["sessions"][name]["state"] in LIVE and not self.host.alive(name):
                    self.record("session.exited", name, {"why": "process gone"}, src="observer")

    def registry(self, pid):
        try:
            with open(os.path.join(self.registry_dir, f"{pid}.json")) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def fire_timers(self):
        now = self.clock()
        for deadline, mid, attempt in reducer.pending_timers(self.state, now):
            # a turn the network cut continues only when the API answers again (core/netprobe.py)
            if deadline <= now and (not mid.startswith(reducer.NET_TIMER) or self._net_up_for(mid)):
                self.record("timer.fired", mid, {"attempt": attempt, "n": attempt})

    def _net_up_for(self, timer):
        """The probe for the cut of the session the n: timer is of: a cut the API refused the network for (net_cut.route)
        waits for the answer of a request it would refuse; the rest, for any answer of the API."""
        cut = (self.state["sessions"].get(timer[len(reducer.NET_TIMER):]) or {}).get("net_cut") or {}
        return self.net_up(True) if cut.get("route") else self.net_up()

    def net_up(self, route=False):
        """Whether the Claude API answers, by the last background probe through the sessions' proxy (core/netprobe.py);
        route — whether it lets this network in (the VPN is back), after a turn it refused the network for."""
        from core import netprobe
        if not hasattr(self, "netprobes"):
            self.netprobes = {}
        if route not in self.netprobes:
            env = lambda: {**platforms.current.owner_env(os.environ), **catalog.user_env()}  # noqa: E731
            self.netprobes[route] = netprobe.Prober(env, probe=netprobe.allowed) if route else netprobe.Prober(env)
        return self.netprobes[route].up()


def claude_command():
    """The claude every process of the hub runs (core/ownclaude): HUB_CLAUDE (a path, also with spaces — Windows profile
    folders — or a command line, parsed as the OS does), the release's own copy in <state>/claude-code, else PATH."""
    return ownclaude.command()


def codex_only(state_dir, name, session_id):
    """Codex (experimental): the session's conversation `session_id` lived in Codex (its id is in the
    session's thread map, hosts/codex.CodexHost._threads_path) and Claude Code has no transcript of it in any project
    folder (it finds one in another folder too, hosts/common.carry_transcript)."""
    try:
        with open(os.path.join(state_dir, "sessions", name, "codex-threads.json"), encoding="utf-8") as f:
            threads = json.load(f)
    except (OSError, ValueError):
        return False
    if not session_id or not isinstance(threads, dict) or session_id not in threads:
        return False
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return not glob.glob(os.path.join(glob.escape(os.path.join(root, "projects")), "*", glob.escape(f"{session_id}.jsonl")))


def codex_signed_in(state_dir, home=None):
    """Codex (experimental): whether the hub's Codex has a sign-in for its sessions — the sign-in file in
    its Codex folder (hosts/codex_set.CONFIG keeps the credentials a file, not the keychain). A key in the environment
    is no sign-in for `codex app-server` (0.162.1: account/read is null with CODEX_API_KEY, OPENAI_API_KEY or
    CODEX_ACCESS_TOKEN set). A stat, no program start: a spawn of a Codex hub asks it (Hub.wait_codex_login), and so
    does «Setup» (modules/setup). A file Codex no longer accepts (a revoked token) is not seen here — its turns fail
    with a 401 and the core tells the owner (codex.lost, codex_tick)."""
    from core import codexcli
    return os.path.isfile(os.path.join(home or codexcli.home_of(state_dir), "auth.json"))   # the Codex pool's active one


def claude_present(cmd):
    """Claude Code's program is there and runnable, as the OS will start it: a name — on PATH; a path — that very file
    (Windows adds only .exe to a path without an extension: a claude.cmd beside it is not what starts). The core
    (Hub.wait_claude) and the page's «Setup» (modules/setup) ask the same."""
    if not os.path.dirname(cmd):
        return bool(shutil.which(cmd))
    ext = os.path.splitext(platforms.current.CLAUDE_EXE)[1]
    tries = [cmd] + ([cmd + ext] if ext and not os.path.splitext(cmd)[1] else [])
    return any(os.path.isfile(p) and os.access(p, os.X_OK) for p in tries)


def make_host(state_dir):
    """The session host per the HUB_HOST setting: stream (default, any OS) or tmux (Mac/Linux). Codex (experimental):
    hub.json "host": "codex" — the whole hub on Codex CLI, claude is not needed (above HUB_HOST); HUB_CODEX=1 — sessions
    on a codex/… model go to Codex, the others to claude."""
    cfg = catalog.config_or_last(state_dir)
    if cfg.get("host") == "codex":
        from hosts.codex import CodexHost, codex_bin
        own = cfg.get("codex") if isinstance(cfg.get("codex"), dict) else {}
        return CodexHost(state_dir, codex=(codex_bin(state_dir),), all_sessions=True, default_model=own.get("model"),
                         default_effort=own.get("effort"))
    kind = os.environ.get("HUB_HOST", "stream")
    if kind == "tmux":
        from core import instance
        from hosts.tmux import TmuxHost
        # an instance of several hubs on a machine has its own tmux server: on one, two hubs saw each other's sessions
        host = TmuxHost(state_dir, claude=claude_command(), **instance.tmux(state_dir))
    else:
        from hosts.streamjson import StreamHost
        host = StreamHost(state_dir, claude=claude_command())
        if os.environ.get("HUB_CODEX") == "1":
            from hosts.codex import CodexHost, MuxHost, codex_bin
            host.claude_from = claude_command
            return MuxHost(host, CodexHost(state_dir, codex=(codex_bin(state_dir),)))
    host.claude_from = claude_command   # the core takes the program anew while it is missing (Hub.claude_program)
    return host


def main():
    from core import perms
    perms.private()
    state_dir = default_state_dir()
    os.makedirs(state_dir, exist_ok=True)
    # an earlier hub left folders open (0755, files 0644) — close them; anything still writable by others after that is not
    # ours, do not start (through the inbox they would write "from the owner", through the hook settings — a command into every session)
    still_open = perms.secure(state_dir, catalog.config_path(state_dir))
    if still_open:
        sys.exit("hubd is not starting: other users can write here — " + ", ".join(still_open) +
                 " (make them yours and closed: chown <your user> <path>; chmod go-w <path>)")
    held = lock.acquire(os.path.join(state_dir, "hubd.lock"))
    if held is None:
        sys.exit("hubd is already running: one writer of the journal")
    refusal = modules.state_file_refusal(state_dir)
    if refusal:   # a core rollback after a module with core.write: the old fold would diverge from the journal (core/modules.py)
        sys.exit(f"hubd is not starting: {refusal}")
    oldlayout.migrate(state_dir)   # the earlier Linux service layout — then as usual: the installer stops the service itself
    hub = Hub(state_dir, make_host(state_dir))   # make_host takes the release's own copy (and its switches) when it is there
    # its version active, the folder tidied up — aside: the check of what a program runs from (lsof) may take a while
    threading.Thread(target=ownclaude.started, args=(state_dir, hub.claude_release), daemon=True).start()

    def stop(*_):
        try:
            hub.count_checked(force=True)   # the inbox check count (core/admit.py) — before the stop snapshot
        except Exception as e:  # noqa: BLE001 — it does not hold up the stop and the snapshot
            print(f"hubd: mailbox check count at stop — {e!r}", flush=True)
        hub.snapshot("stop")  # a restart (a new code version) will begin from this state
        stop_sessions(hub.host, state_dir)
        sys.exit(0)
    platforms.current.on_core_stop(stop, lambda: hub.stopping.append(True))
    serve(hub)
    stop()


def stop_sessions(host, state_dir):
    """Session processes leave together with the core (with the tmux host the sessions live on their own). `hub stop` (the stop file from hub-up) — the hub
    leaves for good: background session commands too; on a restart they live on — sessions continue with them."""
    if hasattr(host, "shutdown"):
        host.shutdown(final=os.path.exists(os.path.join(state_dir, restart.STOP)))


def serve(hub):
    """Ticks until asked to stop between ticks (SIGTERM on POSIX, a pipe from hub-up on Windows); the snapshot
    afterwards is main's."""
    hub.start()
    while not hub.stopping:
        hub.tick()
        time.sleep(0.3)


if __name__ == "__main__":
    main()
