"""Read models on top of the journal — one for all channels (CLI, page, later Telegram).

Pure functions: journal -> what to show. Channels do not derive state themselves, they take it from here.
"""
import collections
import json
import os
import re
import time
import unicodedata
import uuid
from core import appwatch, budget, coretext, corewrite, deputies, deputyroute, executors, flow, forms, hubrun, i18n, journal, minihub, modprompt, reducer, schedule, tools
from core import langdata, limitresume, machines, mcpwatch, owner, parking, refusals, specmode, system
# getattr: a process of v1.4.0 ("Files" during the update's self-check) imports this file with its own core.machines
# in memory, without these names (tests/test_old_page_server)
ENDED = getattr(machines, "ENDED", ("done", "archived"))
# not on the task board of the snapshot (the first frame of /api/stream is light): closed and handed-in tasks; a new
# delegation state is on the board unless named here
OFF_BOARD = (*ENDED, "replied")
INFO_SHOWN = ("model", "effort", "version", "launcher", "harness", "backbone", "launch_generation", "mcp_failed", "mcp_auth")
SLEEP = getattr(machines, "SLEEP", ("asleep", "parked", "retired"))

class _Labels(dict):
    """A label by code — in the process language on access (core/i18n.py): the dict stores catalog keys, not text —
    the language is chosen after import (the page server — the hub language, tests — HUB_LANG)."""

    def __getitem__(self, code):
        return i18n.t(dict.__getitem__(self, code))

    def get(self, code, default=None):
        return self[code] if code in self else default


STATE_LABELS = _Labels({
    "absent": "core.views.state.absent", "starting": "core.views.state.starting", "idle": "core.views.state.idle",
    "busy": "core.views.state.busy", "dialog": "core.views.state.dialog", "held": "core.views.state.held",
    "asleep": "core.views.state.asleep", "closed": "core.views.state.closed", "failed": "core.views.state.failed",
})
MESSAGE_LABELS = _Labels({"queued": "core.views.message.queued", "typing": "core.views.message.typing",
                          "delivered": "core.views.message.delivered", "failed": "core.views.message.failed",
                          "aside": "core.views.message.aside",
                          # delivered, but inserted mid-turn and not read yet (snapshot: inserted_unread)
                          "unread": "core.views.message.unread"})
TIMELINE_LIMIT = 400
MIDTURN = ("busy", "dialog")   # an owner's row arrived while the session's turn ran: it does not start a turn (feed.js)
FLOW_LIMIT = 5000  # "who to whom" signals in page memory (flow animation)
ENDED_KEEP = 1000  # calls marked stopped whose late result is still put on their row (Reader.ended)
REFUSALS_KEEP = 1000  # core rejections of owner messages from the page (the /api/send/receipt receipt) — the latest


# a video is only one sent by hub send-file (owner.file), not one named in the text: the page serves exactly what was sent
VIDEO = re.compile(r"\.(?:mp4|webm|mov)$", re.I)


SNAPSHOT_TAIL = 150  # in a live update — the tail of each session's timeline


def _lang_code(value):
    """The owner's language in the API snapshot — a code from the hub languages; Latin — en (as before languages), unknown — None."""
    from core import i18n
    code = forms.lang_code(value) if isinstance(value, str) else None
    if code == "latin":   # Latin: before languages the snapshot returned en — do not change the field's meaning for clients
        return "en"
    return code if code in i18n.LANGS else None


def shown(state):
    """Session state in the snapshot's words (page, clients, plugins; Telegram): a parked one (core/parking) is free
    for the owner, the process spawns on the first message; a handed-in executor sleeps. Until 10-25 these were flags
    of a sleeping session, the snapshot returns the same shape (state, parked, retired — the API contract)."""
    return {"parked": "idle", "retired": "asleep"}.get(state, state)


def _shown(s):
    """State fields of a snapshot row: state and label from shown, parked only for a parked one, retired always."""
    state = shown(s["state"])
    return {"state": state, "label": STATE_LABELS.get(state, state), "retired": s["state"] == "retired",
            **({"parked": True} if s["state"] == "parked" else {})}


def session_status(s):
    """A session's status in one field — for display (page, plugins, clients), from a ready snapshot row (s: cards already
    in pending, parking is state idle, a sleeping session's background is not given); in order, the first match decides. A failed one is failed,
    a closed one is asleep, by state, before questions. waiting — the owner has something to answer: a form, a permission or a card
    (pending; for a sleeping session too — the answer will wake it) or a tool request (needs-owner) — for a sleeping one too: the owner's decision
    is recorded by the core in any state (10-17). A Claude Code window without a form
    (terminal) is working: the turn is in progress, it can be answered only in the terminal. The watchdog's guess and the session's "what I wait for" are separate fields.
    Manual mode (held), not yet spawned (absent) and an unknown state are idle; a sleeping one is asleep, and so is a closed one."""
    if s["state"] == "failed":
        return "failed"
    if s["state"] == "closed":
        return "asleep"
    if s.get("pending") or any(r.get("state") == "needs-owner" for r in s.get("requests") or []):
        return "waiting"
    if s["state"] in ("busy", "starting", "dialog") or (s["state"] == "idle" and (s.get("background") or s.get("compacting"))):
        return "working"   # a compaction is work too: the process is busy, the next message waits for it
    spec = s.get("spec")   # `/spec on` alone is True; an open spec of an executor is a dict (core/specmode.py)
    if isinstance(spec, dict) and spec.get("ready") and s["state"] in ("idle", "parked", "asleep"):
        return "waiting"   # its spec waits for the owner's "Accept" (core/specmode.py): counted with the cards
    return "asleep" if s["state"] == "asleep" else "idle"


def _spec_row(d, s):
    """An executor's open spec for the snapshot row: {task, path, ready}, or None. ready — it waits for the owner's
    decision now (specmode.awaiting): reported and the executor is not in a turn."""
    return {"task": d["id"], "path": d["spec"]["path"], "ready": specmode.awaiting(d, s)} if d else None


def posts_since(posts, seq):
    """Posts of a plugin channel after number seq (API 1.2): ids are p<seq of the post>."""
    return [p for p in posts if int(p["id"][1:]) > seq]


def old_posts(state_dir, plugin, ids, window):
    """Posts of the plugin channel by number (p<seq>), in the order asked: those in the channel's window (the last
    reducer.FEED_MAX) as they are, the rest read back from the journal by their number (journal.event_at) and made the way
    the reducer makes them. Not found, not a post of this channel, rejected by the core or not valid - skipped."""
    have = {p["id"]: p for p in window}
    # the window holds the channel's latest posts: a number from its oldest one on that is not in it is no post of this
    # channel, and the journal is not read for it
    floor = int(window[0]["id"][1:]) if window else None
    ids = list(dict.fromkeys(ids))
    need = [int(pid[1:]) for pid in ids if pid not in have and (floor is None or int(pid[1:]) < floor)]
    # all the numbers at once: each journal file is read one time, not once per number (20 numbers in one 85 MB archive)
    events = journal.events_at(state_dir, need, accepted=True) if need else {}
    out = []
    for pid in ids:
        post, ev = have.get(pid), events.get(int(pid[1:]))
        if post is None and ev and ev.get("type") == "ext.post" and isinstance(ev.get("data"), dict):
            try:
                got, row = reducer.post_row(ev, ev["data"])
                post = row if got == plugin else None
            except reducer.Rejected:
                post = None
        if post:
            out.append(post)
    return out


class Reader:
    """State and session timelines, assembled as the journal is read (for long-lived channels)."""

    def __init__(self, limit=None):
        self.limit = limit or TIMELINE_LIMIT  # timeline records per session; the archive (core/history.py) gets all
        self.state = reducer.empty()
        self.timelines = {}
        self.tools = {}   # call id -> timeline record (the result is appended to the same one)
        self.ended = {}   # call id -> a record marked stopped: a result that still comes goes on it (ENDED_KEEP latest)
        self.moved = {}   # a busy session renamed -> the name its feed moved to before the core renamed it
        self.todos = {}   # session -> task list (the last TodoWrite)
        self.usage = {}   # session -> the result of the last turn: time, tokens
        self.interrupted = set()  # stopped by the owner: their "turn error" is just a stop
        self.asked = {}   # session -> his compact or restart requests in flight (event type): their outcome is not steps
        self.flow = []    # who wrote to whom: {seq, ts, from, to, kind} — for the flow animation (owner decision 2026-10-02)
        self.turns = flow.Turns()   # the turn result goes to whoever started the turn (core/flow.py)
        # timelines — the page takes only the open session's timeline and only what changed (owner decision 2026-10-03: a 2 MB snapshot on
        # every event): a row has a permanent key k and an edit number rev; a timeline has the number of its last edit
        self.epoch = uuid.uuid4().hex[:8]  # a new reader (page restart, new journal) — timelines anew
        self.rev = 0
        self.keys = 0
        self.tl_rev = {}
        # whether a timeline has anything earlier than what was given ("Show earlier", owner decision 2026-10-03: a button on an empty fork history):
        # born — the reader saw the session from birth; cut — its timeline is cut to limit
        self.born, self.cut = set(), set()
        self.carried = None  # reading a rotation carry: {from — the start of the common tail, own — session -> its first event}
        # send receipt: (addressee, client_id) -> {error, code?, seq} — a core rejection of an owner message from the page;
        # refusing — a rejected message waits for its core.rejected (the reason is in it)
        self.refused, self.refusing = {}, {}

    def feed_all(self, events):
        """A batch of journal events: the outcome of each but the last is known — without a state copy (reducer.verdicts)."""
        for e, known in reducer.verdicts(events):
            self.feed(e, known)

    def touch(self, name, item=None):
        """The timeline of name changed (row item added or edited in place) — a new edit number."""
        self.rev += 1
        if item is not None:
            item["rev"] = self.rev
        if name:
            self.tl_rev[name] = self.rev

    def timeline(self, name, since=0, epoch=None):
        """Timeline rows changed after edit since; full — the whole live timeline (since 0, another reader)."""
        line = self.timelines.get(name) or []
        full = not since or epoch != self.epoch or since > self.rev
        tail = [it for it in line[-SNAPSHOT_TAIL:] if not quiet_line(it)]   # developer-only service rows — only hub show
        items = tail if full else [it for it in tail if it.get("rev", 0) > since]
        # an empty live timeline does not mean "nothing earlier": the journal may have started with a rotation, and the session history lies in the archive (10-13)
        known = bool(line) or (self.state["sessions"].get(name) or {}).get("state") not in (None, "removed")
        # earlier — only if there is something to show there: the page does not get developer-only service rows
        older = known and (name not in self.born or name in self.cut
                           or any(not quiet_line(it) for it in line[:-SNAPSHOT_TAIL]))
        return {"epoch": self.epoch, "rev": self.tl_rev.get(name, 0), "full": full, "older": older,
                "items": json.loads(json.dumps(items, ensure_ascii=False))}

    def drop_removed(self):
        """Timelines and accumulators of deleted sessions go away: the page survives journal rotations without re-reading the journal
        (core/tail), and without this it would hoard everyone who ever existed (a re-read from the start did not bring them back). If a
        session returns (revive) — its past is in the archives (core/history)."""
        gone = {n for n, x in self.state["sessions"].items() if x.get("state") == "removed"}
        gone |= {n for n in self.timelines if n not in self.state["sessions"]}
        for box in (self.timelines, self.todos, self.usage, self.asked, self.tl_rev):
            for name in gone & set(box):
                del box[name]
        self.interrupted -= gone
        self.born -= gone
        self.cut -= gone
        self.tools = {k: v for k, v in self.tools.items() if v.get("session") not in gone}

    def _drop_strays(self):
        """After a rotation the journal tail lands on an old base where some sessions do not exist yet: folding the timeline creates
        messages that never were (10-09: "no session named X" to the hub — with the number of a real check in another session's queue).
        The snapshot knows whose message it really is: an "arrived" row with a foreign id leaves the timeline — unless it
        is a copy that names that very session (_items: words to an executor in the feed of whoever gave the task — "to",
        the bus's words in the executor's feed — "relayed"; 11.11: after a restart they were gone, from the archives too).
        A rename rewrites the message's session, not the name on the copy: that session's former names count too."""
        msgs, sessions = self.state["messages"], self.state["sessions"]

        def named(it, owner):
            return bool({it.get("to"), it.get("relayed")} & {owner, *((sessions.get(owner) or {}).get("was") or ())})
        for name, line in self.timelines.items():
            keep = [it for it in line if not (it.get("kind") == "in" and it.get("id") in msgs
                                              and (owner := msgs[it["id"]].get("session")) not in (None, name)
                                              and not named(it, owner))]
            if len(keep) != len(line):
                self.timelines[name] = keep
                self.touch(name)

    def _refusal(self, event, data):
        """A core rejection of an owner message from the page with a client_id — remember the reason (the latest REFUSALS_KEEP)."""
        if not isinstance(data, dict):
            return
        # the core's core.rejected decides, not this reader's verdict: every such message waits for a possible rejection
        # and so does a card reply (web.answer): the key is the session that was answered
        card = event["type"] == "permission.answered"
        if (event["type"] == "message.received" or card) and data.get("channel") == "web" \
                and data.get("owner") is True and isinstance(data.get("client_id"), str):
            self.refusing[event.get("seq")] = (str((event.get("entity") if card else data.get("to")) or ""), data["client_id"])
            while len(self.refusing) > 50:   # a rejection is written in the same write, right after the fact: only a few are waiting
                del self.refusing[next(iter(self.refusing))]
        elif event["type"] == "core.rejected" and data.get("of") in self.refusing:
            key = self.refusing.pop(data["of"])
            self.refused.pop(key, None)
            # the reason as the core gave it, with the marks of its phrases: refusal() says it in the asker's language
            self.refused[key] = {"error": str(data.get("reason") or ""), **({"l10n": data["l10n"]} if data.get("l10n")
                                                                           else {}),
                                 **({"code": data["code"]} if data.get("code")
                                                                           else {}), "seq": data["of"]}
            while len(self.refused) > REFUSALS_KEEP:
                del self.refused[next(iter(self.refused))]

    def refusal(self, to, client_id, lang=None):
        """The core's rejection of a message to `to` with this client_id, as the reader remembers it: {error, code?, seq} or
        None; error in lang (the core's phrases of it, core/coretext.py; the process language by default)."""
        got = self.refused.get((to, client_id))
        if not got:
            return None
        out = {k: v for k, v in got.items() if k != "l10n"}
        out["error"] = str(coretext.human(coretext.why_of({"reason": got["error"], "l10n": got.get("l10n")}), lang))
        return out

    def feed(self, event, known=None):
        if event["type"] == "session.requested":
            old = self.state["sessions"].get(event.get("entity")) or {}
            req = event.get("data") or {}
            # the same conversation — a revive of the same session_id (hub session revive: a fork after the
            # auto-retire of the fork showed an empty timeline): keep the earlier rows. The name was freed for a COMPLETELY
            # different session — the timeline really is foreign, start anew
            if old.get("state") == "removed" and not (req.get("revive") and req.get("session_id") == old.get("session_id")):
                self.timelines.pop(event.get("entity"), None)
                self.touch(event.get("entity"))
        if event["type"] == "core.snapshot" and (event.get("data") or {}).get("state"):
            why = event["data"].get("why")
            if why == "rotate-start":
                self.carried = {"from": event["data"].get("from"), "own": {}}
            elif why == "rotate" and self.carried:
                self._trim_carried(self.carried)
                self.carried = None
            self.state = reducer.restore(event)  # timelines stay, the state comes from the snapshot
            self._drop_strays()
            return
        if self.carried:
            self.carried["own"].setdefault(journal.session_of(event), event["seq"])
        before = set(self.state["messages"])
        fresh = event["type"] == "session.requested" and \
            (self.state["sessions"].get(event.get("entity")) or {"state": "removed"})["state"] == "removed"
        self.state, _, why = reducer.reduce(self.state, event, known)
        name, data = event.get("entity"), event.get("data") or {}
        self._refusal(event, data)
        if why is None and fresh:
            self.born.add(name)
            self.cut.discard(name)
        if why is None and event["type"] == "turn.tool_result":
            item = self.tools.pop(data.get("id"), None) or self.ended.pop(data.get("id"), None)
            if item:
                item.update(status="error" if data.get("error") else "done", result=data.get("text", ""))
                self.touch(item.get("session"), item)
            return
        if why is None and event["type"] == "turn.tool" and data.get("name") == "TodoWrite" and not data.get("parent"):
            self.todos[name] = (data.get("input") or {}).get("todos") or []
        if why is None and event["type"] == "session.interrupt_requested":
            self.interrupted.add(name)
        if why is None and event["type"] == "turn.result" and data.get("is_error") and name in self.interrupted:
            self.interrupted.discard(name)
            self.turns.feed(event, None)   # the turn ended (a failure — no arrow), as in the history
            self.usage[name] = {k: data.get(k) for k in ("duration_ms", "turns", "tokens_in", "tokens_out", "cost_usd")}
            self._stop_tools(name)
            return
        if why is None and event["type"] == "session.exited":
            self._stop_tools(name)  # the process left — no answer will come (a background agent step)
        if why is None and event["type"] == "turn.result":
            self.usage[name] = {k: data.get(k) for k in ("duration_ms", "turns", "tokens_in", "tokens_out", "cost_usd")}
            self._news(name, event["seq"])
            self._stop_tools(name)  # the turn ended — unclosed calls are no longer "running"
        if why is None and event["type"] == "session.renamed":  # the timeline, tasks and turn result move to the new name
            new = data.get("to")
            if self.moved.get(name) == new and name in self.timelines and new in self.timelines:
                # renamed again while its turn ran: the feed the first rename moved there stays
                moved = self.timelines.pop(new) + self.timelines[name]
                self.timelines[name] = sorted(moved, key=lambda it: it.get("seq", 0))
            for box in (self.timelines, self.todos, self.usage, self.asked):
                if name in box:
                    box[new] = box.pop(name)
            if name in self.state["sessions"]:   # busy: renamed when its turn ends (rename_to), the feed moved now
                self.moved[name] = new
            else:
                self.moved.pop(name, None)
            self.touch(name)
            self.touch(new)
        if why is None:
            self._flow(event, before)
            produced = _items(self.state, event)
            if event["type"] == "session.stop_requested" and name in self.state["sessions"]:
                produced.append((name, self._stopped_row(event, name)))
            said = _owner_command(event)
            if said and name in self.state["sessions"]:   # an owner command — as his row in the timeline, like typed text
                if event["type"] in ("session.compact_requested", "session.reload_requested"):
                    self.asked.setdefault(name, set()).add(event["type"])
                produced.insert(0, (name, {"seq": event["seq"], "ts": event["ts"], "kind": "in", "id": f"c{event['seq']}",
                                           "text": said, "channel": data.get("channel"), "sender": None,
                                           "from": forms.origin_from("owner"),
                                           "command": True,   # mid-turn — does not start a new turn (feed.js)
                                           "midturn": self.state["sessions"][name]["state"] in MIDTURN}))
            self._command_result(event, produced)
            shown = {item.get("id") for _, item in produced if item.get("kind") == "in"}
            for mid in sorted(set(self.state["messages"]) - before - shown):
                # a message created by the core itself (hand over work, "connected", a verdict to the hub, a reply to a session) goes to the
                # recipient's timeline too: otherwise the turn it started would look like a continuation of the previous one (09-30)
                msg = self.state["messages"][mid]
                if msg.get("session") and msg["session"] in self.state["sessions"]:
                    produced.append((msg["session"], core_row(msg, event["seq"], event["ts"], self.state)))
            mod = corewrite.author(event)
            for who, item in produced:   # not name: the event's session is needed below
                if mod:   # a row from a module with core.write (by or interference in handling) — the page draws a badge
                    item["mod"] = mod
                line = self.timelines.setdefault(who, [])
                said = line[-1] if item.get("error") and line else None
                if said and said.get("kind") == "out" and not said.get("error") and said.get("text") == item.get("text"):
                    # a failure result ("You've hit your session limit…") is also written by the host as a reply: it is not the session's answer but the same
                    # error — mark the reply in place, do not repeat it as a second row (10-07)
                    said["error"] = True
                    self.touch(who, said)
                    continue
                self.keys += 1
                item["k"] = self.keys
                self.touch(who, item)
                line.append(item)
                if item.get("kind") == "tool":
                    self.tools[item["id"]] = item
                if len(line) > self.limit:
                    self.cut.add(who)
                del line[:-self.limit]
        # the session sleeps, is closed or failed: its unfinished steps are not "running" — including an output tail that arrived after
        # the process stopped (09-30: a background agent step kept spinning in a sleeping session)
        # (the event's session and those its rows went to: a stop also writes to whoever raised the executor)
        gone = {n for n in (name, *(w for w, _ in produced)) if n} if why is None else {name} - {None}
        for name in sorted(gone):
            if self.state["sessions"].get(name, {}).get("state") not in (*SLEEP, "closed", "failed"):
                continue
            self.asked.pop(name, None)   # there is no process — the command that was awaited will not run any more
            self._stop_tools(name)

    def _stop_tools(self, name):
        """The session's unclosed calls are no longer running (the turn ended, the process left, it sleeps). The result is
        a fact: if it still comes — the call outlived the turn, or the state the reader folds from old events by the current
        code saw the session asleep while it worked — it goes on the same row."""
        for item in list(self.tools.values()):
            if item.get("session") == name:
                item["status"] = "stopped"
                self.touch(item.get("session"), item)
                self.tools.pop(item["id"], None)
                self.ended[item["id"]] = item
        while len(self.ended) > ENDED_KEEP:
            self.ended.pop(next(iter(self.ended)))

    def _stopped_row(self, ev, name):
        """The stop of an executor as a row of its timeline: who stopped it, at which step the turn was cut and how many
        unread lines were withdrawn (reducer: session.stop_requested). Marked command: "Brief" shows it too — it is
        the owner's act (or the hub's), not a service step; code stopped — the page draws it as a stop."""
        data, ts = ev.get("data") or {}, ev["ts"]
        mark = self.state["sessions"][name].get("stopped") or {}
        who = i18n.t("core.views.stopped.owner") if mark.get("by") == "owner" else \
            i18n.t("core.views.stopped.by", name=mark.get("by") or data.get("sender") or "hub")
        steps = sorted((it for it in self.tools.values() if it.get("session") == name), key=lambda it: it.get("ts") or 0)
        if steps:
            minutes = int(max(0, ts - (steps[0].get("ts") or ts)) // 60)
            step = i18n.t("core.views.stopped.step", count=minutes, tool=steps[0].get("name") or "?") if minutes else \
                i18n.t("core.views.stopped.step_short", tool=steps[0].get("name") or "?")
        else:
            step = i18n.t("core.views.stopped.no_step")
        gone = sum(1 for m in self.state["messages"].values() if m["session"] == name and m["state"] == "failed"
                   and m.get("since") == ts and m.get("reason") in ("stopped by owner", "session stopped"))
        text = f"{who} — {step}." + (" " + i18n.t("core.views.stopped.withdrawn", count=gone) if gone else "")
        return {"seq": ev["seq"], "ts": ts, "kind": "sys", "code": "stopped", "command": True, "text": text}

    def _trim_carried(self, carried):
        """The rotation carry (journal.rotate) ended. It holds the common journal tail from `from` and, for each session, its last
        events — even old ones. An old foreign event (the result of a sleeping executor) gives a row in the assigner's timeline too:
        a lone one, hours earlier than the rest; loading the past would go only older than it, and everything between them was lost
       . A session timeline has no holes: from the common tail, if it has rows of this session, otherwise from its own events;
        what is earlier is in the archive (core/history.py)."""
        start = carried.get("from")
        for name, line in self.timelines.items():
            own = carried["own"].get(name)
            floor = start if start is not None and any(it.get("seq", 0) >= start for it in line) else own
            if floor is None or not line or line[0].get("seq", 0) >= floor:
                continue
            for item in line:
                if item.get("seq", 0) < floor and item.get("kind") == "tool":
                    self.tools.pop(item.get("id"), None)
            line[:] = [it for it in line if it.get("seq", 0) >= floor]
            self.cut.add(name)
            self.touch(name)

    def _command_result(self, ev, produced):
        """Core rows about what happened to an owner command (compacting, compacted, failed, respawned) — marked
        command: the page shows them in "Brief" too, next to his row. The same rows from idle compaction, a parked session's
        spawn or a model change are service steps."""
        kind, name, data = ev["type"], ev.get("entity"), ev.get("data") or {}
        if kind in ("host.done", "host.error") and data.get("do") == "compact" or kind == "session.compacted" and "ok" in data:
            asked, last = "session.compact_requested", kind != "host.done" or data.get("result") != "sent"
        elif kind == "session.started" and data.get("source") == "resume":
            asked, last = "session.reload_requested", True
        else:
            return
        mine = self.asked.get(name) or set()
        if asked not in mine:
            return
        for who, item in produced:
            if who == name and item.get("kind") == "sys":
                item["command"] = True
        if last:
            mine.discard(asked)

    def _news(self, name, seq):
        """The result of a turn that the core counts as new for the owner (owner_out) gets news: the page counts "New · N"
        and places the "Last read" line from such rows, not from any answer after seen (an answer to the hub or a result
        sent to another session is not new)."""
        sess = self.state["sessions"].get(name) or {}
        if sess.get("owner_out") != seq:
            return
        turn = sess.get("turn_seq") or 0
        for item in reversed(self.timelines.get(name) or []):
            if item["seq"] < turn:
                return   # this turn said nothing — the earlier result is not its
            if item["kind"] == "out" and not item.get("error"):
                item["news"] = True
                self.touch(name, item)
                return

    def _flow(self, ev, before):
        st, kind = self.state, ev["type"]
        out = []
        for mid in sorted(set(st["messages"]) - before):
            m = st["messages"][mid]
            if m.get("session"):
                out.append((_sender(st, m), m["session"], "task" if m.get("delegation") else "message"))
        to = self.turns.feed(ev, _hub_name(st) if kind == "turn.result" else None)
        if to == "owner" and (st["sessions"].get(ev.get("entity"), {}).get("merged") or {}).get("seq") == ev["seq"]:
            to = None   # the result is not for the owner (reducer._merged_into): no arrow
        if to:
            out.append((ev.get("entity"), to, "result"))
        mod = corewrite.author(ev)
        for frm, to, what in out:
            if frm and to and frm != to:
                self.flow.append({"seq": ev["seq"], "ts": ev["ts"], "from": frm, "to": to, "kind": what,
                                  **({"mod": mod} if mod else {})})
        name = ev.get("entity")
        if kind == "session.remove_requested" and (st["sessions"].get(name) or {}).get("state") == "removed":
            # the session was deleted — from this moment it is not on the "Flow" (owner decision 2026-10-03)
            self.flow.append({"seq": ev["seq"], "ts": ev["ts"], "from": name, "to": name, "kind": "gone"})
        del self.flow[:-FLOW_LIMIT]

    def flow_since(self, seq):
        return [f for f in self.flow if f["seq"] > seq]

    def feed_since(self, plugin, seq):
        """Posts of a plugin channel after number seq (API 1.2)."""
        return posts_since((self.state.get("feeds") or {}).get(plugin) or [], seq)

    def snapshot(self, now=None, *, previous=None):
        """Detached read model. A publisher may supply its previous read-only snapshot to
        reuse unchanged histories; ordinary callers continue to receive independent copies.
        Revision keys include the reader epoch, so a replay cannot reuse another reader's rows.
        """
        import time
        from core import owner
        st = self.state
        sessions = []
        # card questions of each session and those shown by the hub (to whom shown) — one pass per snapshot, not a scan of
        # everyone else's questions for each session: the snapshot is built on every event
        clock = time.time() if now is None else now
        asks = {x["name"]: owner.open_asks(x, clock) for x in st["sessions"].values()}
        live_by = collections.Counter(s.get("raised_by") for s in st["sessions"].values()   # one pass: as in
                                      if executors.takes_place(s))                                # executors.live_of
        shown, taken = {}, minihub.busy_if_needed(st)   # who has work — one pass, if someone waits for a transition
        waits = minihub.waits(st)   # mini-hubs waiting for executors — also one pass
        specs = specmode.open_by_session(st)   # executors' open specs (core/specmode.py) — one pass
        for name, mine in asks.items():
            for a in mine:
                if a.get("shown") and a["shown"] != name:
                    shown.setdefault(a["shown"], []).append({**a, "session": name})
        for s in st["sessions"].values():
            if s["state"] == "removed":
                continue  # deleted — not in the list or the timelines (like a closed tab)
            sessions.append({**{k: s.get(k) for k in ("name", "state", "since", "cwd", "mode", "session_id",
                                                        "restarts", "kind", "tools", "requests",
                                                        "context",   # tokens in the conversation (the last model call)
                                                        "model",     # its own model (hub session model), otherwise none
                                                        "effort",    # its own reasoning level (hub session effort)
                                                        "pid")},
                             # what the page and the clients read from the session's info; the rest (the built-in command
                             # list, plugins, cwd, MCP — hundreds of KB for the whole fleet) stays in the state
                             "info": {k: v for k, v in (s.get("info") or {}).items() if k in INFO_SHOWN} if s.get("info") else s.get("info"),
                             # the session's forms and the hub's questions given as a card (not stale) — the page shows them the same way
                             "pending": (s.get("pending") or []) + asks[s["name"]],
                             # session questions the hub showed in its chat (hub question show): a card for its chat,
                             # the answer goes to them (session). Not in pending: in counters, pushes and for clients there is one question
                             "shown": shown.get(s["name"], []),
                             # closed cards (the latest): {request_id, seq, sig, client_id?} — the core applied the answer
                             # with number seq; the client matches it against the /api/answer receipt (accepted != applied);
                             # how, id, ts, by, why say how and by whom the card was closed (reducer._answered)
                             "answered": s.get("answered") or [],
                             "todos": self.todos.get(s["name"]) or [], "usage": self.usage.get(s["name"]),
                             "recap": s.get("recap"),   # what it is working on now (watchdog, 10-03)
                             "away": s.get("away"),     # Claude Code's recap after a big turn
                             # what it waits for — its own word (hub waiting, 10-03); once it was written to afterwards — stale:
                             # waited — "was waiting", waiting only the fresh one, and all displays read it
                             "waiting": tools.waiting_now(s), "waited": tools.waited(s),
                             "attention": s.get("attention") if s["state"] == "idle" else None,
                             # new: a turn result arrived after the owner looked at the session or wrote to it
                             "unread": (s.get("owner_out") or 0) > (st["owner"].get("seen") or {}).get(s["name"], 0),
                             "last_out": s.get("last_out") or 0, "owner_out": s.get("owner_out") or 0,
                             # the start of that result (<= 160 chars, one line) — what is new, without opening the timeline
                             "out_head": s.get("out_head"),
                             # the time of that result (event ts); null — there was no result or the state is from an earlier version
                             "out_head_ts": s.get("out_head_ts"),
                             # its gist in one line (hub gist, <= 160 chars), while it is about the last result for the owner
                             "gist": tools.gist_now(s, clock),
                             "owner_reply": s.get("owner_reply") or 0,
                             # the start of that answer (<= 160 chars, one line) and its time: a later result the session sent
                             # on its own replaces out_head; null — no answer yet or a state of an earlier version
                             "reply_head": s.get("reply_head"), "reply_ts": s.get("reply_ts"),
                             # the turn was cut by an account limit and the core has not continued it yet: {ts, resets} (resets
                             # - when the window resets, null if the core does not know it), otherwise null
                             "limit_cut": limitresume.view(s.get("limit_cut")),
                             # a new process that has not read its input yet because its MCP servers are coming up (up to
                             # MCP_TIMEOUT): {servers, since, until}, otherwise null - the state itself is a plain "idle"
                             "mcp_wait": parking.mcp_wait(s, clock),
                             # Opus's safety filter moved it to another model (core/refusals.py): {state: waits | again |
                             # back, asked, got, ts, day, again?} - day: such moves on its account in 24 h; null - none
                             "refused": refusals.view(st, s, clock),
                             "seen": (st["owner"].get("seen") or {}).get(s["name"], 0),
                             "role": s.get("role") or "worker", "about": s.get("about"),
                             # its own badge (session.iconed) — on top of the earlier one: old clients see it in icon
                             "icon": s.get("own_icon") or s.get("icon"), "own_icon": s.get("own_icon"),
                             "system": s["name"] in (st.get("system") or []),   # a system agent (core/system.py)
                             "trains": s["name"] == st.get("trainer"),          # agent-dev: "Train executors"
                             # agent-dev: the stops audit of the agents runs and is marked (core/system.stops_view)
                             **({"stops": system.stops_view(st, clock)} if s["name"] == st.get("trainer") else {}),
                             # an executor (spec "tasks, specialists, executors"): who spawned it, the backlog (direction)
                             "raised_by": s.get("raised_by"), "backlog": s.get("backlog"),
                             # stopped at once (session.stop_requested): {ts, by — "owner" or the session that stopped
                             # it}, until it is woken — "stopped by you · 13:02" instead of "handed in"
                             "stopped": s.get("stopped"),
                             # a mini-hub's executor (the bus): the mini-hub the owner's words to it go to — its input
                             # field says so ("goes to <mini-hub>, it passes it on"); null — they go to the session itself
                             # (also while its spec is open: the owner talks to it directly, the mini-hub gets a copy)
                             "relay": None if specmode.direct(s, specs.get(s["name"])) else minihub.hub_of(st, s),
                             # spec mode (core/specmode.py): specs on for this session (/spec on), and an executor's open
                             # spec — {task, path, ready}: ready — it has reported, the spec waits for the owner's "Accept"
                             "specs_on": bool(s.get("spec")), "spec": _spec_row(specs.get(s["name"]), s),
                             # a mini-hub (hub parallelism) that has not switched over yet — what it waits for; "solo": the checkbox and
                             # automatic "solo" — the folder is not git or has no commits (core/minihub.py)
                             "minihub": minihub.on(st, s), "minihub_pending": minihub.pending(st, s, taken),
                             "minihub_dirty": minihub.dirty(st, s),   # uncommitted changes in the mini-hub's folder
                             # solo_auto: the folder's reason (the core's phrase) in the reader's language
                             "solo": bool(s.get("solo")),
                             "solo_auto": coretext.human(minihub.auto(s)) if minihub.lead(s) else None,
                             # its own number of executors (0 — "solo", None — the shared parallelism) and how many places
                             # its live executors take — by the same rule as the cap (executors.live_of)
                             "parallel": minihub.own(s) if minihub.lead(s) else None,
                             "executors_live": live_by.get(s["name"], 0) if minihub.lead(s) else 0,
                             # background commands and agents (core: session.background) — idle, but waiting for them
                             "background": (list((s.get("background") or {}).values())
                                            if s["state"] not in (*SLEEP, "closed", "failed") else [])
                             # its `hub run` commands, outside the process: shown parked too (core/hubrun.py)
                             + hubrun.rows(st, s["name"]),
                             # state, label; parked — idle without a process; retired — handed in and sleeping: messages do not wake it
                             # (owner decision 2026-10-04)
                             **_shown(s),
                             # the conversation is being compacted (the core's /compact or Claude Code's own): since when —
                             # the page says so instead of idle/working (a compaction of a long conversation takes up to a
                             # minute and more); absent otherwise (a client of an older core simply has no such key)
                             **({"compacting": s["compacting"]} if s["state"] in machines.LIVE and parking.compacting(s, clock) else {})})
            # a Claude Code window waits for a human in the terminal and there is no turn form (the tmux host sees the window only via the Claude
            # Code registry): it can be answered only there — hub attach <name>. A core fact, not a display guess
            sessions[-1]["terminal"] = s["state"] == "dialog" and not s.get("pending")
            sessions[-1]["status"] = session_status(sessions[-1])   # from the ready row — without counting twice
            # a mini-hub, while its task is with executors, is not "idle" but "waiting for executors · N + K queued"
            # (owner decision 2026-10-16): one core line for the header, the list and clients
            wait = minihub.waits_label(waits.get(s["name"])) if sessions[-1]["status"] == "idle" else None
            if (sessions[-1]["spec"] or {}).get("ready") and sessions[-1]["status"] == "waiting":
                wait = i18n.t("core.views.spec_waits", id=sessions[-1]["spec"]["task"])   # "waits for you: spec dN"
            if wait:
                sessions[-1]["label"] = wait
                sessions[-1]["label_crew"] = True   # the label is already about executors (the page does not add its own)
        # inserted mid-turn and no turn with its mark has started yet (reducer._unread): Claude Code reads stdin only
        # between steps, so "delivered" would say more than the core knows
        unread = {i for s in st["sessions"].values() for i in s.get("inserted_unread") or ()
                  if (st["messages"].get(i) or {}).get("state") == "delivered"}
        messages = {m["id"]: {"state": m["state"], "inserted_unread": m["id"] in unread,
                              "label": MESSAGE_LABELS.get("unread" if m["id"] in unread else m["state"], m["state"]),
                              "reason": m.get("reason"), "attempt": m["attempt"],
                              "delivered_at": m.get("delivered_at"),  # where the message takes its place in the timeline
                              # queued: Haiku's "urgent?" estimate and whether it can be injected mid-turn (owner decision 2026-10-01)
                              "urgency": m.get("urgency"), "injectable": reducer.injectable(m),
                              # an "FYI" that is already being typed together with its carrier cannot be removed
                              "removable": reducer.removable(m) and (st["messages"].get(m.get("with") or "") or {})
                              .get("state") != "typing",
                              "command": bool(m.get("command")),   # /compact etc.: typed means it is being executed
                              "fyi": bool(m.get("fyi"))}   # `hub send --fyi`: does not start a turn, rides with the next one
                    for m in st["messages"].values()}
        sessions.sort(key=lambda x: (x.get("kind") != "hub",))  # the hub always first
        # runs: what of a plugin runs code — the owner's card names it (core/catalog._runs)
        catalog = {k: {"description": v.get("description", ""), "risk": v.get("risk", []), "kind": v["kind"],
                       **{f: v[f] for f in ("runs", "runs_more") if v.get(f)}}
                   for k, v in st.get("catalog", {}).items()}
        # only open tasks: "replied" ones (hundreds on a long-lived hub) are not read by the page or the clients
        board = [{k: d.get(k) for k in ("id", "session", "for", "state", "since", "words", "context", "spec")}
                 for d in st.get("delegations", {}).values() if d["state"] not in OFF_BOARD]
        o = st.get("owner") or {}
        owner_view = {"state": o.get("state", "present"), "until": o.get("until"), "away_since": o.get("away_since"),
                      # questions without him: "until return" cards and deferred windows
                      "deferred": len(o.get("deferred") or []) + sum(owner.waits_return(a) for x in st["sessions"].values()
                                                                     if x["state"] != "removed" for a in x.get("asks") or [])
                      if o.get("state") == "away" else 0,
                      # the owner's language as a code: a client outside the page (a phone) switches its interface by it
                      # (ru, en, and from the languages — uk, ja… from core/i18n.py LANGS; Latin — null)
                      "lang": _lang_code(o.get("lang")),
                      # away with the flag (core/deputies.py): who takes the questions of their areas, and how many
                      # cards are at them now
                      **({"deputies": [r["person"]["name"] for r in deputies.included(st).values()],
                          "at_deputies": len(deputyroute.at_deputies(st)),
                          # held until /back or the term even while no deputy's channel works (deputies.held)
                          "away_held": deputies.held(st)} if o.get("deputies") else {})}
        visible = {s["name"] for s in sessions}
        # a copy: the reader's state is changed in place (reducer.reduce known=accepted), while the snapshot is handed to other threads
        out = reducer.copy_state({
            "seq": st["seq"], "sessions": sessions, "catalog": catalog, "delegations": board, "owner": owner_view,
            "messages": messages, "schedule": schedule_view(st, now), "browsers": browsers_view(st),
            # executor requests of mini-hubs waiting in the queue — rows under the mini-hub (core/minihub.queue)
            "queue": minihub.queue(st),
            "parallel": minihub.parallel(st),   # the hub-wide parallelism (the default for sessions)
            # limit-saving mode (only with mini-hubs, core/budget.py): {mode, why, until, queued} or None
            "budget": budget.view(st),
            # executor queue pause (hub task pause): {by, why, ts, queued} or None — a banner next to the queue
            "queue_paused": executors.pause_view(st),
            # MCP servers waiting for a login: since when, which sessions need each (core/mcpwatch); a process of the
            # previous release keeps its core.mcpwatch in memory without view and without the waits in the state
            # (tests/test_old_page_server) — nothing waits for it
            "mcp_waits": mcpwatch.view(st) if hasattr(mcpwatch, "view") else [],
            # sign-ins from the page (core/mcpwatch): the state of each attempt, without the link
            "mcp_logins": mcpwatch.logins_view(st) if hasattr(mcpwatch, "logins_view") else [],
            # the "Reactions" module (the page shows it only when enabled): timeline badges (message id — for an owner
            # message, event number — for a session row) and up to which event everything in a session is answered (fold)
            "reactions": {"badges": {**(st.get("badges") or {}), **(st.get("owner_badges") or {})},   # his go on top
                          "taken": {s["name"]: s["taken"] for s in st["sessions"].values() if s.get("taken")},
                          # a reaction fragment on a selection (event number -> start of the piece) — for standing badges; the key is
                          # present only when there is one
                          **({"quotes": q} if (q := {k: v for k, v in (st.get("owner_quotes") or {}).items()
                                                      if (st.get("owner_badges") or {}).get(k)}) else {}),
                          # where the core's thumb on an owner message leads: {session, seq} of the row that answered it;
                          # the key is present only when there is one
                          **({"links": dict(st["badge_links"])} if st.get("badge_links") else {})},
            # session list layout (docs/spec-session-groups.md): None — untouched, {rev} — "Reset" after edits
            "layout": st.get("layout"),
            "limits": st.get("limits") or {}, "account": st.get("account") or {}, "updates": [{k: v for k, v in u.items() if k != "notes"} for u in list(st.get("updates") or [])[-20:]], "update_check": st.get("update_check"), "min_claude": _min_claude(),
            "update_testing": st.get("update_testing"),
            "private_modules": st.get("private_modules"),   # private modules: installed or why not ("About the hub")
            "search_fields": SEARCH_FIELDS,   # timeline search: where to look for text (the page has its own, the same list)
            "epoch": self.epoch, "timeline_rev": {k: v for k, v in self.tl_rev.items() if k in visible},
            # plugin channels (API 1.2): the snapshot has only the last post's number, posts are at /api/feed
            "feeds": {k: int(v[-1]["id"][1:]) for k, v in (st.get("feeds") or {}).items() if v},
            # the core modules' own state (core/modstate.py) — only when there is any; which modules are loaded is decided by
            # the page (modstate.view)
            **({"mods": st["mods"]} if st.get("mods") else {}),
            # modules with core.write that wrote to the core state (core/corewrite.py) — "About the hub"; the key is present only for such hubs
            **({"core_written": corewrite.marks(st)} if st.get("core_written") else {}),
            # the app's code layer (core/appwatch.py): what is edited, the release put back — "About the hub"; only on such hubs
            **({"app_code": appwatch.view(st)} if st.get("app_code") else {}),
            # the move from a git clone (core/appmove.py): the clone and `hub app undo` in "About the hub" for 14 days
            **({"app_move": m} if st.get("app_move") and hasattr(appwatch, "move_view")
               and (m := appwatch.move_view(st, clock)) else {})})
        prior = previous if previous and previous.get("epoch") == self.epoch else {}
        histories, revisions = prior.get("timelines", {}), prior.get("timeline_rev", {})
        out["timelines"] = {
            name: histories[name] if name in histories and revisions.get(name) == self.tl_rev.get(name)
            else reducer.copy_state([it for it in line[-SNAPSHOT_TAIL:] if not quiet_line(it)])
            for name, line in self.timelines.items() if name in visible
        }
        return out


def browsers_view(st):
    """Browsers per the supervisor's facts: whether it answers, which one, who uses it (who has the tool browser:<name>)."""
    out = []
    for name, b in sorted(st.get("browsers", {}).items()):
        users = [s["name"] for s in st["sessions"].values()
                 if s["state"] != "removed" and f"browser:{name}" in (s.get("tools") or [])]
        out.append({"name": name, "tool": f"browser:{name}", "users": users,
                    **{k: b.get(k) for k in ("kind", "url", "up", "idle", "version", "problem", "risk", "since")}})
    return out


SCHEDULE_LABELS = _Labels({"active": "core.views.schedule.active", "paused": "core.views.schedule.paused",
                           "done": "core.views.schedule.done", "expired": "core.views.schedule.expired",
                           "removed": "core.views.schedule.removed"})


def schedule_view(st, now=None):
    """All schedule jobs for channels: when, for whom, the next run (in the owner's time zone)."""
    import time
    from core import schedule
    now = time.time() if now is None else now
    tz = (st.get("owner") or {}).get("tz") or "UTC"
    out = []
    for j in st.get("schedule", {}).values():
        out.append({**{k: j.get(k) for k in ("id", "session", "by", "state", "kind", "prompt", "runs", "skipped",
                                              "last_run", "since", "created", "until", "paused_why", "command",
                                              "last_exit", "check", "quiet", "last_check", "wait")},
                    # why it is paused: the core's phrases in it in the reader's language (schedule.py keeps the marks)
                    "paused_why": coretext.human(coretext.got(j, "paused_why")),
                    "when": schedule.when_text(j, tz), "next": schedule.next_due(j, now, tz),
                    "upcoming": schedule.upcoming(j, now, tz) if j["state"] == "active" else [],
                    "label": SCHEDULE_LABELS.get(j["state"], j["state"]),
                    # who set it as a code (the state keeps the word of the Russian texts: earlier cores compare it) and
                    # a `hub wait` check of an older job told by its text: the page reads no core words
                    "by": _JOB_BY.get(j.get("by"), j.get("by")),
                    **({"wait_gist": g} if (g := forms.wait_gist(j.get("prompt"))) else {})})
    out.sort(key=lambda j: (j["state"] not in ("active", "paused"), j["next"] or 9e12, j["id"]))
    grp = schedule.groups([j for j in out if j["state"] in ("active", "paused")])
    for j in out:
        j["group"] = grp.get(j["id"], j["id"])  # one task — one item on the page (owner decision 2026-10-01)
    return out


# the sender of an in timeline row (_who): an identifier the page names itself (who.js, feed.js FROM) — :core,
# :schedule, :owner (a session name is Latin with no ":", reducer.NAME: never one of them), mod:<name> / ext:<name>
# (who.js outside), a session name. The parse of an archive made by an earlier core keeps the Russian words of then
# (language data, core/langdata.py): sender_now reads them as the codes
CORE_SENDER, SCHEDULE_SENDER, OWNER_SENDER = ":core", ":schedule", ":owner"
_WHO = {"core": CORE_SENDER, "hub": "hub", "schedule": SCHEDULE_SENDER, "owner": OWNER_SENDER, "module": "mod:{name}",
        "plugin": "ext:{name}", "session": "{name}", "member": ":member"}   # a member of the Telegram group
_OLD_WHO = langdata.legacy("who")
_OLD_SENDER = {_OLD_WHO["core"]: CORE_SENDER, _OLD_WHO["schedule"]: SCHEDULE_SENDER, _OLD_WHO["owner"]: OWNER_SENDER}
_JOB_BY = {_OLD_WHO["owner"]: OWNER_SENDER, _OLD_WHO["core"]: CORE_SENDER}   # schedule_view: a job's by in the state
_SENDER_WORDS = {CORE_SENDER: "cli.show.core", SCHEDULE_SENDER: "cli.show.schedule", OWNER_SENDER: "cli.show.owner"}
_OLD_NAMED = ((_OLD_WHO["module"].split("{", 1)[0], "mod:"), (_OLD_WHO["plugin"].split("{", 1)[0], "ext:"))


def sender_word(sender):
    """A row's sender in the reader's words (hub show): the codes and mod:/ext: by the catalog, a session by its name."""
    s = str(sender or "")
    if s in _SENDER_WORDS:
        return i18n.t(_SENDER_WORDS[s])
    if s.startswith(("mod:", "ext:")):
        return i18n.t("cli.show.module" if s.startswith("mod:") else "cli.show.plugin", name=s[4:])
    return s


def job_by_word(by):
    """Who set a schedule job, in the reader's words (hub schedule list): the job's `by` in the state is the word the owner
    or the core had when it was set (_OLD_WHO), the catalog names them now; a session name stays as is."""
    return sender_word(_JOB_BY.get(by, by))


def sender_now(rows):
    """The rows of an archive parse with the senders of this core: an earlier parse wrote the core, the schedule, the
    owner and module X / plugin X as Russian words (_OLD_WHO); a session name stays as is. Rows are changed in place."""
    for it in rows if isinstance(rows, list) else ():
        s = it.get("sender") if isinstance(it, dict) else None
        if not isinstance(s, str) or not s:
            continue
        if s in _OLD_SENDER:
            it["sender"] = _OLD_SENDER[s]
        else:
            for head, code in _OLD_NAMED:
                if head and s.startswith(head):   # a session name is Latin (reducer.NAME): never one of these
                    it["sender"] = code + s[len(head):]
    return rows


def _from(msg):
    """(kind, name) of the sender: the from field, without it — the text label (core/forms.py); no label — (None, None)."""
    f = forms.sender(msg)
    return (f["kind"], f.get("name")) if f else forms.parse_origin(msg.get("origin"))


def core_row(msg, seq, ts, st):
    """The timeline row of a message the core created itself (a message of state st), at event seq."""
    # the core writing in the owner's name about his action on a card (an answer, a closed or withdrawn card): the row is
    # the core's, not the owner's words — clients draw rows from the owner as his own bubbles (feed audit 05.11). The
    # reducer marks such a note itself (on_behalf); a note written before the mark keeps its old row
    behalf = msg.get("on_behalf")
    return {"seq": seq, "ts": ts, "kind": "in", "id": msg["id"], "text": msg["text"], "channel": "core",
            "sender": CORE_SENDER if behalf else _who(msg),   # no label — the core, as in _who
            "from": forms.origin_from("core") if behalf else _from_field(msg) or forms.origin_from("core"),
            **({"on_behalf": behalf} if behalf else {}),
            **({"label": lbl} if (lbl := row_label(msg)) else {}),
            # a retelling of the owner's reply to a card — a "↳" row next to it
            **({"answer_to": msg["answers_question"]} if msg.get("answers_question") else {}),
            # the words of another hub: the page draws them as a thread with that hub
            **({"hubbus": hb} if (hb := _hubbus(st, msg)) else {})}


def row_label(msg, data=None):
    """The label of an in feed row — what the page shows instead of text, which stays as the session read it: a short
    line for a button press or an answer to a card (_feed_label), else the core's phrases of the text in the hub's
    language (core/coretext.py); neither — None. The live feed and the archive parse (core/history.py) both take it;
    data is the event's own (the pressed button's caption), when the caller has it. The row is the same whoever folds
    it (the live feed, the page's archive parse): a session's own process prints the core's lines from text instead
    (core_words) — the tag and the task and message numbers are what it acts on."""
    return _feed_label(msg, data) or coretext.shown(msg, i18n.current())


def core_words(it):
    """Whether an in row is a line of the core itself (REPLIED, STOPPED, TIMEOUT, a reminder on a task — not a retelling
    of the owner's answer to a card): its label is the words the owner reads, its text the form a session acts on,
    with the tag and the numbers. A session's own process (HUB_SESSION: `hub show`, `hub show --grep`) prints the
    text; a search finds such a row by either (search_texts): "d13" lies only in the text."""
    return it.get("kind") == "in" and it.get("sender") == CORE_SENDER and not it.get("answer_to")


def _session_form(it):
    """A session's own process (HUB_SESSION) reads the core's line in its form, with the tag and the numbers (core_words)."""
    return bool(os.environ.get("HUB_SESSION")) and core_words(it)


def _words(it):
    """The words of an in row for the reader: the label (a short line, the core's phrases in the reader's language)
    when there is one; the text — the owner's own row, and the core's lines in a session's own process (core_words)."""
    if it.get("label") and (it.get("sender") or it.get("to")) and not _session_form(it):
        return it["label"]
    return it.get("text")


def _from_field(msg):
    """The from field of an in timeline row — who sent it, {kind, name?}, not from the label text (API 1.26); no field and no label — None
    (each in row has its own default sender: owner, core, hub, schedule)."""
    kind, name = _from(msg)
    return forms.origin_from(kind, name) if kind else None


def _who(msg):
    """Who a message created by the core is from ("from the core", "from the hub", "from <session>"; no label — the core)."""
    kind, name = _from(msg)
    return _WHO[kind].format(name=name) if kind else _WHO["core"]  # sender identifier


def _sender(st, msg):
    """Who wrote the message (reducer._origin and others): no label — the owner; a label without a preposition — the label itself."""
    kind, name = _from(msg)
    if kind is None:
        origin = (msg.get("origin") or "").strip()
        return origin or "owner"
    if kind == "hub":
        return _hub_name(st) or "hub"
    return _SENDER[kind].format(name=name)


# the "Flow" node by sender is an identifier, not a label: as label parsing gave it before the fields, not translated
_SENDER = {"owner": "owner", "core": "core", "schedule": "schedule", "module": forms.NAMED_LABEL["module"],
           "plugin": forms.NAMED_LABEL["plugin"], "session": "{name}", "member": "group member"}


def _hub_name(st):
    return next((n for n, s in st["sessions"].items() if s.get("kind") == "hub"), "hub")


def _hm(ts, o):
    from core import owner
    return owner._local(ts, o.get("tz") or "UTC").strftime("%H:%M")


def _questions(st):
    from core import owner
    return owner.questions(st)


def _reminder_md(st, qid):
    from core import owner
    return owner.reminder_md(st, qid)


IMAGE = re.compile(r"\.(png|jpe?g|gif|webp)$", re.I)


def _min_claude():
    from core import version
    return version.MIN_CLAUDE


OWNER_COMMANDS = ("session.compact_requested", "session.reload_requested", "session.model_set", "session.effort_set")


def _owner_command(ev):
    """An owner command to the core (/compact, /reload, /model, /effort — the input field, buttons, hub session …) — as text, the way he
    would have typed it: commands go to the core as a separate fact, not as session text, and would vanish from the timeline without this — here
    it shows that the owner sent it."""
    kind, data = ev["type"], ev.get("data") or {}
    if kind not in OWNER_COMMANDS:
        return None
    if not tools.from_owner(data):
        return None
    if kind == "session.compact_requested":
        return " ".join(["/compact", str(data.get("text") or "").strip()]).strip()
    if kind == "session.reload_requested":
        return "/reload"
    if kind == "session.model_set" and data.get("model"):
        return f"/model {data['model']}"
    if kind == "session.effort_set" and data.get("level"):
        return f"/effort {data['level']}"
    return None


# service rows nobody needs except the developer (~800 a day on a big hub): spawn after parking,
# creating and deleting a session, the handoff protocol, the start of compaction (the "compacted N->N" result stays)
# (parsing earlier records without code: their Russian text is language data, core/langdata.py)
QUIET_SYS = set(langdata.legacy("quiet_sys"))
_JOB_LINE = re.compile(r"^(?:" + langdata.legacy("job_line") + r")([^,\s]+)")
# the same rows via the code field (core/forms.py): the row text is translated, while the decision is by code; QUIET_SYS and _JOB_LINE are for
# records without a code (journal archive parses made before it)
QUIET = {"created", "removed", "resumed", "compacting", "handoff"}


def quiet_line(it):
    """A timeline row that neither the page, nor plugins, nor clients receive (`hub show` shows it): the rows above and
    service rows about `hub wait` checks in the schedule. An owner command and an error — never. Judged by the record alone: archive
    parses made before this rule are judged the same way. A quiet "-" result is not here: it tells the page
    that the turn has no answer (feed.js)."""
    if it.get("kind") != "sys" or it.get("command") or it.get("error"):
        return False
    code = it.get("code")
    if code == "job":
        return bool(schedule.WAIT_ID.match(str(it.get("job") or "")))
    if code in QUIET:
        return True
    text = it.get("text") or ""
    if text in QUIET_SYS:
        return True
    m = _JOB_LINE.match(text)
    return bool(m and schedule.WAIT_ID.match(m[1]))


def shown_line(it):
    return not quiet_line(it)


# Timeline search of a session (the page: what is loaded — on its own, the past — /api/history/search): which row fields are searched
# for text, by row kind. A field is what the timeline shows in "Details"; dicts and lists — their strings at any depth
# (a step argument, answers to a question). "a|b" — the first non-empty one: a short string (label) is shown instead of the raw text.
# Words the page writes itself ("Allowed") are not searched: the searched text must lie in the record — so the archive
# parse filter (history.search) drops a chunk without it, without reading JSON. The page takes the list from the snapshot.
SEARCH_FIELDS = {
    "in": ["label|text", "quote", "words", "context", "attachments"], "out": ["text"], "sys": ["text"],
    "tool": ["name", "input", "result"], "file": ["caption", "name"], "notify": ["title", "text"], "ask": ["input"],
    "answer": ["answers"], "terminal": ["text"], "remind": ["text"], "reply": ["text", "quote", "task"], "bus": ["text"],
    "summary": ["hub", "deputies", "deputies_waiting", "decided", "waiting", "done"],
}


def fold(s):
    """Text for comparison: case-insensitive, the Russian letters "yo" and "ye" are equal (the page does the same, js/feedsearch.js)."""
    return langdata.fold(s.lower())


def _strings(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from _strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from _strings(x)


def search_texts(it):
    """Strings of a timeline record in which text is searched (SEARCH_FIELDS) — each separately. The core's own line
    (core_words) is searched in its text too, but only in a session's own process (HUB_SESSION, like _words): it acts
    on the task and message numbers, which only the text has. The owner's terminal and the page read the words, and
    search what they show: the label (the page's own search takes the first non-empty field, js/feedsearch.js)."""
    both = _session_form(it)
    for field in SEARCH_FIELDS.get(it.get("kind"), ()):
        names = field.split("|")
        for name in names:
            if it.get(name):
                yield from _strings(it[name])
                if not both:
                    break


def found(it, needle):
    """Whether needle (already fold-ed) is in one of the record's fields — the first such field, else None."""
    for s in search_texts(it):
        if needle in fold(s):
            return s
    return None


BUTTON_MAX = 80   # the caption of a pressed button: a few words, never a task text


def _feed_label(msg, data=None):
    """A short timeline row instead of the service text of an owner message: a button press (action) and a reply to a
    card (answers_question) — not the full "Answer to question …: …". The message text is left alone: "Send
    again" and delivery go by it (10-14). A press whose text is a task for a session (the page's "Fix" on the core's
    own edits, "Check and install" a plugin…) names the button in data["button"]: the row is the button's caption,
    not the whole task as if the owner wrote it (11-07)."""
    if msg.get("action"):
        button = (data or {}).get("button")
        shown = button.strip() if isinstance(button, str) and 0 < len(button.strip()) <= BUTTON_MAX else msg["text"]
        return i18n.t("core.views.feed_pressed", text=shown)
    if msg.get("answers_question") and msg.get("answer_body"):   # his words without the head (as the bus sends them)
        return i18n.t("core.views.feed_answer", text=coretext.said(msg))
    return None


def _sender_of(data):
    """The author of an in timeline row, other than the owner: a session — its name, a module and a plugin process — mod:<name> / ext:<name> (for
    earlier plugin facts the name has no prefix); the owner and the core — None (the page draws them its own way)."""
    sender = data.get("sender")
    if not (isinstance(sender, str) and sender):
        return None
    if data.get("channel") == "session":
        return sender
    return forms.outside_id(sender) if data.get("channel") == "plugin" else None


def _member_of(data):
    """An in row's `member` field: {name} of the Telegram group's member whose words these are (the Telegram module names them,
    reducer._group_note; an admin's words are the owner's and carry none) — a field of its own, not a sender: the row is not a
    session's. {} when there is none."""
    who = reducer.group_person(data, "member") if data.get("channel") == "telegram" and data.get("owner") is False else None
    return {"member": {"name": reducer.group_name(who)}} if who else {}


def _first_reply(st, d, seq):
    """Is this the executor's first result for this owner message? Only it goes as a quote ("replied to your message"); the count is
    results for the task (delegations.on_result, "-" does not count) and results of other tasks of the same session for the same message."""
    return d.get("replies", 1) <= 1 and not any(
        o is not d and o["session"] == d["session"] and o["for"] == d["for"] and o.get("replies") and o["replied_seq"] < seq
        for o in st["delegations"].values())


AGENT_MAX = 40
ZW_JOIN = "\u200c\u200d"   # zero-width joiners: legitimate in compound emoji and in the writing of some languages


def _agent_of(data):
    """The name of the external agent on whose behalf a core module relayed the message (the agent field of the module's
    fact, e.g. agent-relay) — one line, no longer than AGENT_MAX; None means not an agent letter. Only on a module's
    fact (channel plugin, sender mod:<name>, not the owner — that is how the core writes it, core/spool.outsider): the
    page, Telegram, the owner's words from a module handle, sessions and plugin processes (through `hub` commands)
    cannot pass as an agent. Longer than AGENT_MAX or with control and invisible characters (a writing direction change,
    a zero-width space) is not a name (we neither cut nor clean it: two agents would merge into one). The core does not
    parse a label in the text."""
    sender, agent = _sender_of(data), data.get("agent")
    if not (sender and sender.startswith("mod:") and data.get("owner") is not True):
        return None
    return _agent_name(agent)


def _agent_name(agent):
    """AGENT_MAX and the characters of `_agent_of`, for a name another side chose (a module's agent, the other hub's agent)."""
    if not isinstance(agent, str):
        return None
    if any((unicodedata.category(c) in ("Cc", "Zl", "Zp") and c not in " \t\n\r")
           or (unicodedata.category(c) == "Cf" and c not in ZW_JOIN) for c in agent):
        return None   # the byline "agent X · module Y" is one line: a module cannot reorder or hide "module Y" from X
    agent = " ".join(agent.split())   # checked before: split() would turn other separators into spaces
    return agent if 0 < len(agent) <= AGENT_MAX else None


def _hubbus(st, msg):
    """The words of another hub in the core's frame (busstate._deliver, _to_liaison) as fields for the page — whose hub they
    are from, who wrote there and the words alone, so that the page does not take them out of the frame by their text;
    None — not such a message, or its inbox record is gone."""
    hb = msg.get("hubbus")
    item = ((st.get("bus") or {}).get("inbox") or {}).get(hb.get("inbox")) if isinstance(hb, dict) else None
    if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(hb.get("contact"), str):
        return None
    agent = _agent_name(item.get("agent_from"))
    return {"contact": hb["contact"], "body": item["text"], **({"agent": agent} if agent else {}),
            **({"thread": item["thread"]} if isinstance(item.get("thread"), str) else {}), **({"direct": True} if item.get("direct") else {})}


def _items(st, ev):
    """Timeline records an event produces: [(session, record)]."""
    kind, name, data, ts = ev["type"], ev.get("entity"), ev.get("data") or {}, ev["ts"]
    base = {"seq": ev["seq"], "ts": ts}
    if kind == "message.received":
        msg = st["messages"].get(f"m{ev['seq']}")
        if msg and msg["session"]:
            # a short row for the timeline (text stays raw — "Send again" sends it); the core's phrases added to the
            # words (a reply's tail) — in the hub's language
            lbl = row_label(msg, data)
            agent = _agent_of(data)
            # no field and no label — the owner, if the fold decided so (from_owner: the flag and its channel); otherwise unknown (null)
            frm = _from_field(msg) or (forms.origin_from("owner") if msg.get("from_owner") else None)
            items = [(msg["session"], {**base, "kind": "in", "id": msg["id"], "text": msg["text"],
                                       **({"label": lbl} if lbl else {}),
                                       "attachments": msg.get("attachments") or [],
                                       # failed to download from Telegram — as a fact next to the words, not inside them
                                       **({"attach_failed": msg["attach_failed"]} if msg.get("attach_failed") else {}),
                                       # "Send again" on the page repeats with the same quote, reply and wake
                                       **{k: data[k] for k in ("quote", "reply_to", "wake") if data.get(k)},
                                       # the board cards he named: chips under the words; "Send again" repeats them
                                       **({"tasks": msg["tasks"]} if msg.get("tasks") else {}),
                                       "channel": data.get("channel") or ev.get("src"),
                                       "sender": _sender_of(data),
                                       **_member_of(data),   # the words of a member of the Telegram group: not the owner's
                                       # the owner wrote to a session that is gone or never was: the words went to the hub
                                       # or to whoever raised a removed executor (reducer._received decides, the row only
                                       # carries it): a client shows whom he wrote to
                                       **({"addressed": msg["addressed"]} if msg.get("addressed") else {}),
                                       # a letter from an external agent via a module: the byline "agent X · module Y" comes from the field
                                       **({"agent": agent} if agent else {}),
                                       "from": frm})]
            # the owner's session asked the liaison to pass words to another hub (busstate._request): in its own timeline the page
            # draws them as the first words of a thread with that hub ({kind: bus}; a page that does not know it shows a service row)
            asker = _sender_of(data)
            if msg.get("bus_to") and asker and asker != msg["session"] and asker in st["sessions"]:
                items.append((asker, {**base, "kind": "bus", "id": msg["id"], "dir": "out", "contact": msg["bus_to"],
                                      "text": data.get("text") or ""}))
            d = st.get("delegations", {}).get(data.get("reply_to") or "")
            mirror = (st["messages"].get(d["for"], {}).get("session") or "hub") if d and d["for"] else None
            # the mirror goes only to another timeline: a letter to a sleeping executor is handed by the core to whoever spawned it (the hub), and
            # the owner's word is also with the hub — otherwise one timeline gets two identical rows
            if mirror and mirror != msg["session"]:
                items.append((mirror, {
                    **base, "kind": "in", "id": msg["id"], "text": msg["text"],
                    **({"label": lbl} if lbl else {}), "channel": data.get("channel"),
                    "to": msg["session"], "sender": None, "from": frm}))
            # the bus: words for a mini-hub's executor went to the mini-hub (reducer._received) — its feed has them with
            # the "Owner to executor …" note, the executor's feed has the words as written, marked whom they were passed to
            # (27.10: "stop" written on the executor's page went to the mini-hub, and that page did not say so)
            via = msg.get("relayed_for")
            if via and via != msg["session"] and via in st["sessions"]:
                # the same row (author, channel, attachments, quote), minus what belongs to the mini-hub's copy: its
                # short label and "Send again" with reply/wake; midturn — the executor's turn was running (feed.js)
                row = {k: v for k, v in items[0][1].items() if k not in ("label", "reply_to", "wake")}
                items.append((via, {**row, "text": data.get("text") or "", "relayed": msg["session"],
                                    "midturn": st["sessions"][via]["state"] in MIDTURN}))
            return items
        return []
    if kind == "bus.direct_requested":
        # a session's words straight to an own hub (busstate._direct): the core sends them itself, the liaison and its
        # `message.received` are not in the way — so this is the only row of them: the first words of the thread with that
        # hub in the sender's feed, as the ones handed to the liaison above (the page joins the hub's answer to it)
        asker = data.get("sender")
        if asker and asker in st["sessions"] and data.get("contact") and isinstance(data.get("text"), str):
            return [(asker, {**base, "kind": "bus", "id": f"m{ev['seq']}", "dir": "out", "contact": data["contact"],
                             "text": data["text"]})]
        return []
    if kind == "owner.notify":  # a monitor wrote to the owner — into the hub timeline
        return [(_hub_name(st), {**base, "kind": "notify", "title": data.get("title") or data.get("source") or i18n.t("core.views.monitor"),
                                 "text": data.get("text", ""), "urgent": bool(data.get("urgent"))})]
    if kind == "owner.file":  # hub send-file — a file to the owner into the timeline of whoever sent it: a screenshot is visible on the page
        # and to plugins, not only in Telegram (owner decision 2026-10-06)
        path = str(data.get("path") or "")
        # from a session — by its name, not by state: after a journal rotation the tail lands on a snapshot taken before the tail, in which
        # a session born later does not exist — the file went to the hub timeline (10-22: the owner saw nothing)
        own = data.get("channel") == "session" and data.get("sender")
        who = data["sender"] if own or data.get("sender") in st["sessions"] else _hub_name(st)
        # a mini-hub's executor — into the mini-hub's timeline: the owner talks only to it (the bus, core/minihub), 10-22
        sent = who
        who = minihub.hub_of(st, st["sessions"].get(who)) or who
        row = {**base, "kind": "file", "path": path, "caption": data.get("caption"),
               # a copy in attachments (hub send-file) — under the name the session gave it
               "name": re.split(r"[/\\]", str(data.get("source") or path))[-1],
               "image": bool(IMAGE.search(path)), "video": bool(VIDEO.search(path)),
               **({"source": str(data["source"])} if data.get("source") else {})}
        items = [(who, {**row, **({"by": sent} if sent != who else {})})]   # whose file — the mini-hub timeline shows it is the executor's
        # a file from a schedule job's command (20.11: a sleeping session's job — no turn, no mark, the file lay in a feed
        # nobody opens): the owner's news like `hub notify` from a job — the hub timeline has it too, "sent you by" whom
        hub = _hub_name(st)
        if data.get("schedule_job") and own and who != hub:
            items.append((hub, {**row, "by": sent}))
        return items
    if kind == "owner.away":
        o = st.get("owner", {})
        until = o.get("until")
        return [(_hub_name(st), {**base, "kind": "sys", "text": i18n.t("core.views.away_until", time=_hm(until, o))
                                 if until else i18n.t("core.views.away")})]
    if kind == "owner.back" or (kind == "timer.fired" and ev.get("entity") == "owner"):
        sm = st.get("owner", {}).get("summary")
        if not (sm and sm.get("seq") == ev["seq"]):
            return []
        waiting = [{**{k: v for k, v in w.items() if k != "text_l10n"},
                    "text": coretext.human(coretext.got(w, "text"), i18n.current())} if isinstance(w, dict) else w
                   for w in sm.get("waiting") or []]
        return [(_hub_name(st), {**base, "kind": "summary", **sm, "waiting": waiting})]
    if kind == "bus.decline_requested":   # the liaison declined another hub's request: a fact of the core, not the model's result
        from core import busstate, liaison
        item = ((st.get("bus") or {}).get("inbox") or {}).get(data.get("inbox")) or {}
        if item.get("outcome") == "declined" and item.get("done") == ts:
            c = ((st.get("bus") or {}).get("contacts") or {}).get(item.get("contact")) or {}
            name = busstate.shown(c) or item.get("contact") or ""
            why = item.get("why") or i18n.t("core.views.bus_no_why")
            return [(liaison.NAME, {**base, "kind": "sys", "code": "hubbus_declined",
                                    "text": i18n.t("core.views.bus_declined", name=name, why=why)})]
        return []
    if kind == "owner.decided":
        return [(data.get("sender") or "hub", {**base, "kind": "sys", "text": i18n.t("core.views.decided", text=data.get("text"))})]
    if kind == "owner.decided_revoked":
        rec = next((d for d in st.get("decisions") or [] if d["seq"] == data.get("seq")), None)
        if not rec or (rec.get("revoked") or {}).get("ts") != ev["ts"]:   # refused or a repeat: nothing was withdrawn
            return []
        return [(data.get("sender") or rec.get("by") or "hub",
                 {**base, "kind": "sys", "text": i18n.t("core.views.decided_revoked", seq=rec["seq"], text=rec["text"])})]
    if kind == "timer.fired" and str(ev.get("entity", "")).startswith("q:"):
        q = next((q for q in _questions(st) if q[0] == ev["entity"]), None)
        if q and st["owner"]["reminders"].get(q[0]) == (data.get("n")):
            text = coretext.human(q[3], i18n.current())
            # text: the one line, as before (search, plugins); md: the same for reading — a paragraph per question
            item = {**base, "kind": "remind", "session": q[1], "text": text, "md": _reminder_md(st, q[0]) or text,
                    "n": data.get("n")}
            return [(q[1], item)] + ([(_hub_name(st), item)] if q[1] != _hub_name(st) else [])
        return []
    if kind == "delegation.created":
        d = st.get("delegations", {}).get(f"d{ev['seq']}")
        if not d:
            return []
        from core import delegations
        hub = data.get("sender") or "hub"
        span = delegations.brief_of(st["messages"][d["message"]])   # the setter's words inside text, the rest is the core's frame
        return [(d["session"], {**base, "kind": "in", "id": d["message"], "text": st["messages"][d["message"]]["text"],
                                "channel": "session", "sender": hub, "from": _from_field(st["messages"][d["message"]]),
                                "task": True, "words": d["words"], "context": d["context"], **({"brief": span} if span else {})}),
                (hub, {**base, "kind": "sys", "text": i18n.t("core.views.task_to_for", id=d["id"], session=d["session"],
                                                             **{"for": d["for"]})
                       if d["for"] else i18n.t("core.views.task_to", id=d["id"], session=d["session"])})]
    if kind == "schedule.due":
        msg = st["messages"].get(f"m{ev['seq']}")
        if msg and msg.get("schedule"):
            # the job and what a check waits for — let the page show them in human terms, without a service label
            from core import schedule
            job = st.get("schedule", {}).get(msg["schedule"]) or {}
            return [(msg["session"], {**base, "kind": "in", "id": msg["id"], "text": msg["text"], "channel": "schedule",
                                      "sender": _WHO["schedule"], "job": msg["schedule"],  # identifier
                                      "from": forms.origin_from("schedule"),
                                      "waiting": schedule.waiting_for(job)})]
        return []
    if kind in ("schedule.set", "schedule.remove_requested", "schedule.retarget"):
        job = st.get("schedule", {}).get(ev.get("entity"))
        if not job:
            return []
        from core import schedule
        text = i18n.t("core.views.job.set", id=job["id"], when=schedule.when_text(job, st["owner"]["tz"])) \
            if kind == "schedule.set" else i18n.t("core.views.job.removed", id=job["id"]) \
            if kind == "schedule.remove_requested" else i18n.t("core.views.job.retarget", id=job["id"])
        return [(job["session"], {**base, "kind": "sys", "text": text, "code": "job", "job": job["id"]})] \
            if job["session"] in st["sessions"] else []
    if kind in ("secret.filled", "secret.used") and name in st["sessions"]:
        # "Secrets" (modules/secrets): what the session got out of the hub's vault, by name — the owner sees each use
        item = str(data.get("item") or "")
        text = i18n.t("core.views.secret.filled", item=item, origin=str(data.get("origin") or "")) \
            if kind == "secret.filled" else i18n.t("core.views.secret.used", item=item, program=str(data.get("program") or ""))
        return [(name, {**base, "kind": "sys", "text": text})]
    if kind == "secret.saved" and name in st["sessions"]:
        # `hub secret add` (spec d2441132): the session put a value into the vault or changed one — the owner sees it,
        # and from which file; the previous value stays for "Return" on the Secrets tab
        item, source, key = str(data.get("item") or ""), str(data.get("source") or ""), str(data.get("key") or "")
        source = f"{source} ({key})" if key else source   # a file and its line: "path (KEY)"
        text = i18n.t("core.views.secret.added", item=item, source=source) if "new" in (data.get("changed") or []) \
            else i18n.t("core.views.secret.changed", item=item, source=source)
        return [(name, {**base, "kind": "sys", "text": text})]
    if kind in ("session.said", "turn.tool") and data.get("parent"):
        return []   # a step or text of a helper agent — under its Agent step ("Agent steps"), not in the session timeline
    if kind == "session.said":
        text = reducer.shown_text(data.get("text", ""))   # the HANDOFF-DONE mark is for the core, not the owner
        return [(name, {**base, "kind": "out", "text": text})] if text.strip() else []
    if kind == "turn.tool":
        if data.get("name") == "TodoWrite":
            return []  # tasks are shown as a list above the input field, as in Claude Code
        return [(name, {**base, "kind": "tool", "id": data.get("id"), "session": name, "name": data.get("name"),
                        "input": data.get("input") or {}, "status": "running", "result": None})]
    if kind == "session.recap" and data.get("by") in ("claude", "codex"):  # the host's recap (Claude Code's /recap; on Codex
        # the core's model over the thread's tail, hosts/codex_life.recap) — as a timeline row, like in Claude Code.
        # reducer.HOST_RECAP as a literal: the page server of the previous release keeps its old reducer in memory
        return [(name, {**base, "kind": "sys", "text": f"※ recap: {data.get('text') or ''}"})]
    if kind == "session.compact_requested":
        if data.get("why") == "idle":
            return [(name, {**base, "kind": "sys", "text": i18n.t("core.views.compact.idle")})]
        # when to compact is decided by the core (reducer._pump) — the timeline does not predict: "compacting" appears when the core really sent the command
        # (host.done below); the owner's request is visible as a separate /compact row (_owner_command above)
        return []
    if kind == "host.done" and data.get("do") == "compact" and data.get("result") == "sent":
        return [(name, {**base, "kind": "sys", "text": i18n.t("core.views.compact.sending"), "code": "compacting"})]
    if kind == "host.done" and data.get("do") == "compact":   # tmux: only the stream-json host can do /compact
        return [(name, {**base, "kind": "sys", "text": i18n.t("core.views.compact.tmux")})]
    if kind == "host.error" and data.get("do") == "compact":
        return [(name, {**base, "kind": "sys", "text": i18n.t("core.views.compact.failed")})]
    if kind == "session.compacted" and "ok" in data:   # from the host, with numbers; the SessionStart(compact) hook — no row
        k = lambda n: f"{round(n / 1000)}k" if isinstance(n, (int, float)) else "?"
        return [(name, {**base, "kind": "sys", "text": i18n.t("core.views.compact.done", before=k(data.get("before")),
                                                               after=k(data.get("after")))
                        if data.get("ok") else i18n.t("core.views.compact.failed")})]
    if kind == "session.interrupt_requested":
        return [(name, {**base, "kind": "sys", "text": i18n.t("core.views.interrupted")})]
    if kind == "turn.result":
        items = []
        if (data.get("text") or "").lstrip().startswith("HANDOFF-DONE") and st["sessions"].get(name, {}).get("state") in SLEEP:
            items.append((name, {**base, "kind": "sys", "text": i18n.t("core.views.handoff_asleep")}))
        if data.get("is_error"):
            items.append((name, {**base, "kind": "out", "error": True,
                                 "text": reducer.shown_text(data.get("text")) or i18n.t("core.views.turn_error")}))
        for d in st.get("delegations", {}).values():
            if d.get("replied_seq") == ev["seq"] and d["for"]:
                # the executor's reply to the owner — into the conversation where the owner asked (the hub timeline), under his message
                owner_msg = st["messages"].get(d["for"], {})
                words = " ".join((d.get("words") or "").split())
                # the quote of his message goes on the first result for the task; the following ones (mini-hub interim results,
                # wake-ups) — as the same session row, with a short note on which task (10-25, as in Telegram)
                reply = {**base, "kind": "reply", "from": name, "for": d["for"], "delegation": d["id"],
                         "text": reducer.shown_text(data.get("text", ""))}
                if not _first_reply(st, d, ev["seq"]):
                    reply["again"] = True
                    if words:
                        reply["task"] = words[:30] + ("…" if len(words) > 30 else "")
                else:
                    reply["quote"] = (d.get("words") or "")[:160]
                items.append((owner_msg.get("session") or "hub", reply))
        return items
    if kind == "permission.requested" and data.get("auto") == "allow" and ev.get("src") == "host":
        inp = data.get("input") or {}
        what = str(inp.get("command") or inp.get("file_path") or inp.get("path") or "").split("\n")[0][:120]
        why = f" ({data['why'][:160]})" if data.get("why") else ""
        text = i18n.t("core.views.auto_allowed", what=f"{data.get('tool')} {what}").strip() + why
        return [(name, {**base, "kind": "sys", "text": text})]
    if kind == "permission.requested":
        return [(name, {**base, "kind": "ask", "request_id": data.get("request_id"), "tool": data.get("tool"),
                        "input": data.get("input") or {}})]
    if kind == "permission.answered":
        # the hub's answer for the owner while he was away (spec d2549241) is not his: its own channel mark
        # (only the hub's is accepted with a why from a session: the core refuses everyone else's, core/reducer._hub_answers)
        hub = data.get("channel") == "session" and bool(data.get("why"))
        return [(name, {**base, "kind": "answer", "request_id": data.get("request_id"),
                        "behavior": data.get("behavior"), "answers": data.get("answers"),
                        "channel": "hub" if hub else data.get("channel") or ev.get("src")})]
    if kind.startswith("tools."):
        tool = tools.resolve(st, st["sessions"].get(name), data.get("tool"))   # the short name — as the core understood it
        req = next((r for r in reversed(st["sessions"].get(name, {}).get("requests", [])) if r["tool"] == tool), None)
        text = {
            "tools.requested": lambda: i18n.t("core.views.tools.requested", tool=tool, why=data.get("why") or "").strip(": "),
            "tools.granted": lambda: i18n.t("core.views.tools.waiting", tool=tool) if req and req["state"] == "needs-owner"
            else i18n.t("core.views.tools.had", tool=tool) if req and req.get("had") == ev.get("seq")
            else i18n.t("core.views.tools.granted_words", tool=tool, text=req["owner_words"]["text"][:80],
                        message=req["owner_words"]["message"])
            if req and req.get("owner_words") and data.get("for") else i18n.t("core.views.tools.granted", tool=tool),
            "tools.approved": lambda: i18n.t("core.views.tools.approved_had", tool=tool) if req and req.get("had") == ev.get("seq")
            else i18n.t("core.views.tools.approved", tool=tool),
            "tools.denied": lambda: i18n.t("core.views.tools.denied_reason", tool=tool, reason=data["reason"])
            if data.get("reason") else i18n.t("core.views.tools.denied", tool=tool),
            "tools.revoked": lambda: i18n.t("core.views.tools.revoked", tool=tool),
        }.get(kind)
        text = text() if text else None
        return [(name, {**base, "kind": "sys", "text": text})] if text else []
    if kind == "turn.started" and not data.get("marker"):
        # a background task finished — Claude Code inserted <task-notification> itself: a service row, not input (10-05)
        if "notice" in data or (data.get("prompt_head") or "").lstrip().startswith("<task-notification>"):
            done = (data.get("notice") or {}).get("summary")
            return [(name, {**base, "kind": "sys", "text": i18n.t("core.views.background_done_summary", summary=done)
                            if done else i18n.t("core.views.background_done")})]
        return [(name, {**base, "kind": "terminal", "text": data.get("prompt_head", "")})]
    sys_key = {
        "session.requested": "core.views.sys.created",
        "session.exited": "core.views.sys.exited",
        "session.start_stuck": "core.views.sys.start_stuck",
        "session.take_requested": "core.views.sys.take",
        "session.give_requested": "core.views.sys.give",
        "session.close_requested": "core.views.sys.close",
        "session.close_ready": "core.views.sys.close_ready_summary" if data.get("summary") else "core.views.sys.close_ready",
        "session.wake_requested": "core.views.sys.wake",
        "session.remove_requested": "core.views.sys.remove",
        "session.ended": "core.views.sys.ended",
    }.get(kind)
    sys_text = i18n.t(sys_key, summary=data.get("summary")) if sys_key else None
    code = {"session.requested": "created", "session.close_requested": "handoff",
            "session.remove_requested": "removed"}.get(kind)
    if kind == "session.requested" and name in st["sessions"] and st["sessions"][name].get("ever_started"):
        sys_text, code = i18n.t("core.views.sys.revived"), None
    # the core retired the executor (state retired) — not "fell asleep"; what counts as leaving is decided by the core alone (reducer._closing)
    if kind == "session.close_requested" and (st["sessions"].get(name) or {}).get("state") != "retired":
        if (st["sessions"].get(name) or {}).get("slept_on") == ev["seq"]:
            # the CLOSING automaton put it to sleep at once, without starting the handoff (a parked one — no process; one taken into manual mode)
            sys_text, code = i18n.t("core.views.sys.slept_user") if data.get("owner") \
                else i18n.t("core.views.sys.slept_hub"), None
    if kind == "session.started" and data.get("source") == "resume":
        sys_text, code = i18n.t("core.views.sys.resumed"), "resumed"
    if sys_text and name in st["sessions"]:
        return [(name, {**base, "kind": "sys", "text": sys_text, **({"code": code} if code else {})})]
    return []


# --- `hub show`: what a session did and wrote (owner decision 2026-09-30) -------------------------------------

def _clock(ts, tz, year=False):
    import datetime
    from core.zones import zone
    return datetime.datetime.fromtimestamp(ts, zone(tz)).strftime("%d.%m.%Y %H:%M" if year else "%d.%m %H:%M")


def _one_line(text, n=300):
    line = " ".join(str(text or "").split())
    return line if len(line) <= n else line[:n] + "…"


def _whole(text, n=None):
    """The text uncut, with its own lines; continuation lines are indented so a record reads as one."""
    return str(text or "").strip().replace("\n", "\n    ")


def _tool_arg(inp):
    inp = inp or {}
    for key in ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "prompt"):
        if inp.get(key):
            return str(inp[key]).split("\n")[0]
    return next((str(v) for v in inp.values() if isinstance(v, str)), "")


def _item_line(it, full=False):
    return (f"[mod:{it['mod']}] " if it.get("mod") else "") + _item_text(it, full)


def _item_text(it, full=False):
    one = _whole if full else _one_line   # full: whole text, with its lines (hub show --full)
    kind = it.get("kind")
    if kind == "in":
        via = f" → {it['relayed']}" if it.get("relayed") else ""   # the words went to the mini-hub, not here (_items)
        # the words of the core or of a session with the core's phrases in them: in the reader's language (label,
        # core/coretext.py) — the owner's in his terminal; a session's own process reads the core's lines as it got
        # them, with the tag and the numbers (_words); the owner's own row keeps his words
        return f"{sender_word(it.get('sender')) if it.get('sender') else i18n.t('cli.show.owner')}{via}: {one(_words(it))}"
    # the lines below are in the reader's language too: a session's process reads English, the owner's terminal his
    if kind == "out":
        who = i18n.t("core.views.line.error" if it.get("error") else "core.views.line.session")
        return f"{who}: {one(it.get('text'))}"
    if kind == "reply":
        return i18n.t("core.views.line.replied_owner", name=it.get("from"), text=one(it.get("text")))
    if kind == "tool":
        mark = {"error": "core.views.line.tool_error", "running": "core.views.line.tool_running",
                "stopped": "core.views.line.tool_stopped"}.get(it.get("status"))
        return f"{it.get('name')}: {one(_tool_arg(it.get('input')), 160)}{i18n.t(mark) if mark else ''}"
    if kind == "ask":
        qs = (it.get("input") or {}).get("questions") or []
        return i18n.t("core.views.line.asks_owner",
                      text=one(" / ".join(q.get("question", "") for q in qs) or it.get("tool")))
    if kind == "notify":
        return f"{it.get('title')}: {one(it.get('text'))}"
    return one(it.get("text") or kind)


def recent(events, keep=1500):
    """Events for the recent timeline: from the last snapshot that is followed by at least keep events (the whole journal through
    the fold takes minutes: on 10-01 hub show did not fit in 2 minutes)."""
    for i in range(max(0, len(events) - keep), -1, -1):
        if events[i]["type"] == "core.snapshot" and (events[i].get("data") or {}).get("state"):
            return events[i:]
    return events


def show(reader, name, n=20, tz=None, earlier=(), full=False):
    """A session's state, its background and the last n timeline records — as lines; no session — []. earlier — records from the journal
    archives (core/history.py), if the live timeline is shorter than n. full — whole records, not cut."""
    snap = reader.snapshot()
    sess = next((s for s in snap["sessions"] if s["name"] == name), None)
    if not sess:
        return []
    tz = tz or (reader.state.get("owner") or {}).get("tz")
    out = [f"{name} — {sess['label']} · {sess.get('cwd') or ''}"]
    out += modprompt.lines(reader.state["sessions"].get(name))   # what modules changed in its prompt (Module API)
    # a helper agent with the model its call named (the page's row says "3 helpers · <model>")
    out += [i18n.t("core.views.line.bg_agent_model", title=b.get("title") or b["id"], model=b["model"])
            if b.get("kind") == "agent" and b.get("model") else
            i18n.t("core.views.line.bg_agent" if b.get("kind") == "agent" else "core.views.line.bg_command",
                   title=b.get("title") or b["id"]) for b in sess.get("background") or []]
    out += [f"{_clock(it['ts'], tz)}  {_item_line(it, full)}" for it in [*earlier, *reader.timelines.get(name, [])][-n:]]
    return out


def grep(reader, pattern, n=30, tz=None, full=False, only=None, older=()):
    """Who wrote about pattern and when (case-insensitive): messages, replies, steps of all sessions (only — the sessions
    of that list), newest at the bottom. older — (session, record) from the journal archives (core/history.py), found by
    its own search. full — what was found, whole, not cut."""
    tz = tz or (reader.state.get("owner") or {}).get("tz")
    needle, hits = str(pattern).lower(), []
    for name, line in reader.timelines.items():
        for it in line if only is None or name in only else ():
            text = _item_line(it)
            if needle in text.lower() or needle in str(it.get("text") or "").lower():
                hits.append((it["ts"], f"{name}  {_clock(it['ts'], tz)}  {_item_line(it, True) if full else text}"))
    for name, it in older:
        hits.append((it["ts"], f"{name}  {_clock(it['ts'], tz, year=True)}  {_item_line(it, full)}"))
    return [h for _, h in sorted(hits)[-n:]]


REF = re.compile(r"([mqd])([0-9]+)")   # a journal record: a message, a question card, a task


def card_state(state_dir, ev, state, now):
    """Where the question card of the permission.requested event ev stands, `hub show qN`: {"state": …}. Sure only what
    the core knows: open (it waits for the owner: a card in the session's asks not yet stale, or a permission form),
    answered ("at": the form's answer fact; "words": the owner's own words that closed it), dismissed (the owner's x),
    withdrawn (by the asker, the hub or the owner, with "why"), expired (still in the session's cards but the owner's
    list no longer holds it, owner.ASK_TTL), gone (the session is removed and still holds the card among its cards).
    Anything else - the card left the state and no record, no fact and no words of the owner say how - is "closed":
    that it was answered is not said, nor that it was not. The order: the state of the core, the session's record of the last closed cards, the messages of the
    state, then the journal (the cards it no longer keeps). "by", "ts" - who closed it and when."""
    data, name = ev.get("data") or {}, ev.get("entity")
    rid, qid = str(data.get("request_id") or ""), f"q{ev['seq']}"
    sessions = (state or {}).get("sessions") or {}
    sess = sessions.get(name) or next((s for s in sessions.values() if name in (s.get("was") or [])), None)
    live = sess if sess and sess.get("state") != "removed" else None   # a removed session's cards stay in its record
    if live:
        ask = next((a for a in live.get("asks") or [] if a.get("id") == qid), None)
        if ask:
            if ask not in owner.open_asks(live, now):
                return {"state": "expired"}
            return {"state": "open", **({"deputy": ask["deputy"], "qid": qid} if deputyroute.at(ask) else {})}
        if rid and any(r.get("request_id") == rid for r in live.get("pending") or []):
            return {"state": "open"}   # a permission form: the same wait
    rec = next((r for r in (sess or {}).get("answered") or [] if rid and r.get("request_id") == rid or r.get("id") == qid), None)
    def words():
        # the owner's own words that answered it (not the core's note in his name: on_behalf, nor the note of the x)
        return next((m for m in ((state or {}).get("messages") or {}).values()
                     if m.get("from_owner") and not m.get("on_behalf") and not m.get("card_dismissed")
                     and (m.get("answers_question") == qid or rid and m.get("answered_form") == rid)), None)
    if rec and rec.get("how") in ("dismissed", "withdrawn"):
        return {"state": rec["how"], "ts": rec.get("ts"), "by": rec.get("by"), "why": rec.get("why")}
    if rec and rec.get("how") == "answered":
        at = journal.event_at(state_dir, rec["seq"], accepted=True) if isinstance(rec.get("seq"), int) else None
        at = at if at and at["type"] == "permission.answered" and (at.get("data") or {}).get("request_id") == rid else None
        said = None if at else words()
        return {"state": "answered", "ts": rec.get("ts"), **({"at": at} if at else {}), **({"words": said} if said else {})}
    said = words()
    if said:
        return {"state": "answered", "ts": said.get("created"), "words": said}
    got = journal.closing_of(state_dir, ev["seq"], rid)
    if got:
        gd = got.get("data") or {}
        how = {"question.dismissed": "dismissed", "question.withdrawn": "withdrawn"}.get(got["type"], "answered")
        by = "owner" if tools.from_owner(gd) else gd.get("sender") if isinstance(gd.get("sender"), str) else None
        return {"state": how, "ts": got["ts"], "by": by, "why": str(gd.get("why") or "").strip() or None, "at": got}
    # gone: the removed session still holds it among its cards; one it no longer holds was closed some way not kept
    gone = not live and any(a.get("id") == qid for a in (sess or {}).get("asks") or [])
    return {"state": "gone" if gone else "closed"}


def _bus_card(ev, state):
    """A card of the bus (busstate._card) as the question it is: its qN is the core's bus event, the card an ask of the
    liaison or the hub (request_id bus-…), answered and closed like any card. No session holds it or its record — no
    request_id: card_state says only that it no longer waits."""
    qid = f"q{ev['seq']}"
    name, held = next(((n, a) for n, s in ((state or {}).get("sessions") or {}).items()
                       for a in [*(s.get("asks") or []), *(s.get("answered") or [])] if a.get("id") == qid), (None, {}))
    return {**ev, "type": "permission.requested", "entity": name,
            "data": {"request_id": held.get("request_id"), "tool": "AskUserQuestion", "interactive": True,
                     "input": ((ev.get("data") or {}).get("card") or {}).get("input") or {}}}


def _card_line(card, tz):
    """The state of a card as a line (cli.show.card_state): no empty brackets and no "?" for what is not known."""
    how = card["state"]
    at = i18n.t("cli.show.card.at", when=_clock(card["ts"], tz, year=True)) if card.get("ts") else ""
    by = i18n.t("cli.show.owner") if card.get("by") == "owner" else card.get("by")
    why = _one_line(card.get("why")) if card.get("why") else ""
    reason = {"dismissed": lambda: i18n.t("cli.show.card.why_dismissed"),
              "withdrawn": lambda: i18n.t("cli.show.card.why_withdrawn_by" if by else "cli.show.card.why_withdrawn", by=by),
              "expired": lambda: i18n.t("cli.show.card.why_expired", hours=owner.ASK_TTL // 3600),
              "gone": lambda: i18n.t("cli.show.card.why_gone")}.get(how)
    dep = card.get("deputy")   # a card at a deputy: "open" while it waits for their answer, not "open" once they answered (nobody waits then)
    if dep and how == "open":
        return i18n.t("cli.show.card_state", state=i18n.t("cli.show.card.answered_deputy", qid=card["qid"], name=dep.get("name") or "?")
                      if dep["state"] == deputyroute.ANSWERED else i18n.t("cli.show.card.open_routed", name=dep.get("name") or "?"))
    if not reason:
        return i18n.t("cli.show.card_state", state=i18n.t(f"cli.show.card.{how}"))
    said = " ".join(x for x in (reason(), at if how in ("dismissed", "withdrawn") else "") if x) + (f": {why}" if why else "")
    return i18n.t("cli.show.card_state", state=i18n.t("cli.show.card.because", state=i18n.t("cli.show.card.no_answer"),
                                                      reason=said))


def entry(state_dir, ref, reader=None, tz=None, now=None):
    """One journal record whole, `hub show mN|qN|dN`: who, to whom, when, the whole text; a question card also says where
    it stands (right after the first line and as the last line). The number is the event number in the journal (live or
    archived); no such record or an event of another kind — []."""
    m = REF.fullmatch(ref)
    if not m:
        return []
    ev = journal.event_at(state_dir, int(m[2]), accepted=True)   # the journal keeps facts the core rejected
    tz = tz or ((reader.state.get("owner") or {}).get("tz") if reader else None)
    kind, data = m[1], (ev or {}).get("data") or {}
    if kind == "m" and ev and ev["type"] == "message.received":
        frm = data.get("sender") or (i18n.t("cli.show.owner") if data.get("owner") else "?")
        out = [i18n.t("cli.show.entry_head", ref=ref, when=_clock(ev["ts"], tz, year=True), frm=frm,
                      to=data.get("to") or "?")]
        if data.get("for"):
            out.append(i18n.t("cli.show.for_message", ref=data["for"]))
        if data.get("quote"):
            out.append(i18n.t("cli.show.in_reply_to", text=_whole(data["quote"])))
        return out + ["", str(data.get("text") or "").strip()]
    msg = ((reader.state.get("messages") or {}).get(ref) if reader and kind == "m" else None)
    if msg:   # a message the core created (a task, a result, a card answer): its event is not message.received
        who, name = _from(msg)
        frm = name or (i18n.t("cli.show.owner") if who == "owner" else who or "?")
        return [i18n.t("cli.show.entry_head", ref=ref, when=_clock(msg["created"], tz, year=True), frm=frm,
                       to=msg.get("to") or msg.get("session") or "?"), "", str(msg.get("text") or "").strip()]
    if not ev:
        return []
    when = _clock(ev["ts"], tz, year=True)
    if kind == "q" and ev["type"].startswith("core.bus_") and isinstance((data.get("card") or {}).get("input"), dict):
        # a question card of the bus (busstate._card); a contact's card (core.bus_join, core.bus_link_used) is no question
        ev = _bus_card(ev, reader.state if reader else None)
        data = ev["data"]
    if kind == "q" and ev["type"] == "permission.requested":
        out = [i18n.t("cli.show.entry_head", ref=ref, when=when, frm=ev.get("entity") or "?", to=i18n.t("cli.show.owner"))]
        card = card_state(state_dir, ev, reader.state if reader else None, time.time() if now is None else now)
        out.append(_card_line(card, tz))   # right after the first line: `| head` does not hide it
        inp = data.get("input") or {}
        asked = inp.get("questions") if data.get("tool") == "AskUserQuestion" else None
        if not asked:
            out += ["", f"{data.get('tool')}: {json.dumps(inp, ensure_ascii=False)}"]
        for q in asked or []:
            out += ["", f"{q.get('header') + ': ' if q.get('header') else ''}{q.get('question', '')}"]
            out += [f"  - {o.get('label')}" + (f" — {o['description']}" if o.get("description") else "")
                    for o in q.get("options") or []]
        got, words = card.get("at"), card.get("words")
        if got and got["type"] == "permission.answered":
            answers = (got.get("data") or {}).get("answers") or {}
            said = "; ".join(f"{k} → {v}" for k, v in answers.items()) or (got.get("data") or {}).get("behavior") or ""
            out += ["", i18n.t("cli.show.answer", when=_clock(got["ts"], tz, year=True), text=said)]
        elif words:   # the owner's words to the session closed it: no form answer in the journal
            out += ["", i18n.t("cli.show.answer", when=_clock(words["created"], tz, year=True),
                               text=str(coretext.said(words) or "").strip())]
        else:
            out += ["", out[1]]
        return out
    if kind == "d" and ev["type"] == "delegation.created":
        out = [i18n.t("cli.show.entry_head", ref=ref, when=when, frm=data.get("sender") or data.get("by") or "?",
                      to=data.get("session") or "?")]
        d = ((reader.state.get("delegations") or {}).get(ref) if reader else None) or {}
        if d.get("state"):
            out.append(i18n.t("cli.show.task_state", state=d["state"]))
        out += ["", str(data.get("context") or "").strip()]
        if d.get("reply"):
            out += ["", i18n.t("cli.show.task_reply", text=_whole(d["reply"] if isinstance(d["reply"], str)
                                                                   else json.dumps(d["reply"], ensure_ascii=False)))]
        return out
    return []
