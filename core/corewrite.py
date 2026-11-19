"""The core.write capability (Module API, spec C9): a module writes core events and intercepts their handling.

- A module fact in the inbox (the top-level field module) from a ready module with core.write — of any type except core.*,
  module.* and other modules' mod.*: hubd writes it with the fields by: <name> and src: mod:<name> (admit). Without the
  capability — as before.
- Interception: for an event of a type from the manifest's fold, hubd calls /hook/fold before writing (the event and a narrow
  part of the state) — the answer is core | after | replace | reject and patch edits; with "actions": true also
  /hook/actions with the ready list of actions of this record. The module's decision goes in the same journal line, in the
  field fold; the core code folds what was written (folded) for all readers, they do not need the module. Only hubd
  executes an action edit: it does not change state.
- A module failure (timeout, garbage, the fold with edits rejected, the check) — the core folds the event, module.error
  is written on the next line; a skip (pass budget, suspended, not ready) is a fold.skipped mark in the line.
- The mark: the file <state>/core-written and state.core_written appear before a module's first write with by or an
  intervention; core code that does not understand such records does not start on them (core/modules.core_write_refusal).
The owner's consent is the list modules.core_write in hub.json: without it a module with core.write is blocked.
"""
import json
import os
import re
import time
from collections import deque

import platforms
from core import forms, i18n, modrun, modules, version
from core.coretext import T
from core.modrun import BadResult
from core.tools import Rejected

CAP = "core.write"
FORMAT = 1                 # the core_written mark format this code understands (modules.CORE_WRITE_FORMAT)
PARTS = ("session", "queue", "owner", "account", "budget", "limits", "runs", "mods")
MODES = ("core", "after", "replace", "reject")
# roots an edit may not touch (and anything starting with "_")
CLOSED = ("seq", "core_written", "modules", "mods", "mod_dedup", "web_dedup", "config_seen", "app_code", "app_move")
FORBIDDEN = ("core.", "module.", "mod.")              # journal and module machinery: do not intercept
MAX_PATCH, MAX_FOLD, MAX_PART, MAX_REASON = 256, 256 << 10, 256 << 10, 300
CALL = 1.0                 # budget of a /hook/fold and /hook/actions call, s
PASS = 1.0                 # the total time a module added to hubd per tick pass, s
SHARE, WINDOW = 0.10, 60.0  # and no more than this share over a sliding window
TYPE = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*(?:\.\*)?")
NEW_ID = re.compile(rf"m(\d+){forms.MSG_TAIL}")   # a core message id — by the label rule


# --- manifest and graph (core/modules.py) ---------------------------------------------------------------------------

def manifest_errors(man):
    """What is wrong with the C9 manifest fields: fold, fold_state, actions."""
    errs = []
    caps = man.get("capabilities") if isinstance(man.get("capabilities"), list) else []
    fold = man.get("fold", [])
    if not (isinstance(fold, list) and all(isinstance(t, str) and TYPE.fullmatch(t) for t in fold)):
        errs.append('fold: a list of event types or families "x.*", e.g. ["message.received", "timer.*"]')
        fold = []
    bad = [t for t in fold if t.startswith(FORBIDDEN)]
    if bad:
        errs.append(f"fold: {', '.join(bad)} cannot be intercepted — they are journal and module machinery")
    if fold and CAP not in caps:
        errs.append(f"fold: intercepting the fold needs the {CAP} capability in capabilities")
    parts = man.get("fold_state", [])
    if not (isinstance(parts, list) and all(p in PARTS for p in parts)):
        errs.append("fold_state: parts of the state from " + ", ".join(PARTS))
    acts = man.get("actions", False)
    if not isinstance(acts, bool):
        errs.append("actions: true or false")
    elif acts and not fold:
        errs.append("actions: editing actions works on the intercepted types (fold)")
    return errs


def _family(p):
    return p[:-1] if p.endswith(".*") else None


def covers(pattern, kind):
    """Type kind falls under a type or a family "x.*" from fold."""
    fam = _family(pattern)
    return kind.startswith(fam) if fam else pattern == kind


def overlap(a, b):
    fa, fb = _family(a), _family(b)
    if fa and fb:
        return fa.startswith(fb) or fb.startswith(fa)
    if fa or fb:
        return covers(a, b) if fa else covers(b, a)
    return a == b


def resolve_rules(res, settings, block):
    """Graph rules for core.write (modules.resolve, before dependencies): without the owner's consent (modules.core_write)
    — blocked; two modules intercept overlapping types — both blocked unless modules.use.fold picked one."""
    agreed = set(settings.get("core_write") or [])
    for r in res:
        if r["state"] is None and CAP in (r["manifest"].get("capabilities") or []) and r["name"] not in agreed:
            block(r, i18n.t("core.corewrite.needs_consent", cap=CAP))
    live = [r for r in res if r["state"] is None and r["manifest"].get("fold")]
    chosen = (settings.get("use") or {}).get("fold")
    why = {}
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            both = next((x for x in a["manifest"]["fold"] for y in b["manifest"]["fold"] if overlap(x, y)), None)
            if both is None:
                continue
            if chosen in (a["name"], b["name"]):
                loser = b if chosen == a["name"] else a
                why.setdefault(id(loser), (loser, i18n.t("core.corewrite.fold_taken", kind=both, chosen=chosen)))
            else:
                for r in (a, b):
                    why.setdefault(id(r), (r, i18n.t("core.corewrite.fold_clash", kind=both, a=a["name"], b=b["name"])))
    for r, text in why.values():
        block(r, text)


# --- module answers (modrun.POINTS) ---------------------------------------------------------------------------------

def _ops(patch, seq):
    if not isinstance(patch, list) or len(patch) > MAX_PATCH:
        raise BadResult(f"fold: patch is a list of up to {MAX_PATCH} edits")
    out = []
    for op in patch:
        if not (isinstance(op, dict) and op.get("op") in ("set", "del") and not set(op) - {"op", "path", "value"}):
            raise BadResult('fold: an edit is {"op": "set"|"del", "path": [...], "value"}')
        path = op.get("path")
        if not (isinstance(path, list) and path and all(isinstance(k, str) and k for k in path)):
            raise BadResult("fold: path is a non-empty list of string keys")
        if path[0] in CLOSED or path[0].startswith("_"):
            raise BadResult(f"fold: {path[0]} cannot be changed by an edit")
        if (op["op"] == "set") != ("value" in op):
            raise BadResult("fold: value goes with set and only with it")
        m = NEW_ID.fullmatch(path[1]) if path[0] == "messages" and len(path) > 1 else None
        if m and isinstance(seq, int) and int(m[1]) > seq:
            raise BadResult(f"fold: {path[1]} is the number of a future core message (event {seq})")
        out.append({"op": op["op"], "path": list(path), **({"value": op["value"]} if op["op"] == "set" else {})})
    return out


def fold_answer(res, ctx):
    """The /hook/fold answer: {mode, patch, reason} -> normal form or BadResult."""
    if not isinstance(res, dict) or set(res) - {"mode", "patch", "reason"}:
        raise BadResult("fold: {mode, patch, reason}")
    mode = res.get("mode", "core")
    if mode not in MODES:
        raise BadResult("fold: mode — " + " | ".join(MODES))
    if mode == "reject":
        reason = res.get("reason")
        if not (isinstance(reason, str) and reason.strip()) or len(reason) > MAX_REASON or res.get("patch"):
            raise BadResult(f"fold: reject needs a reason of up to {MAX_REASON} characters and no patch")
        return {"mode": mode, "reason": reason.strip()}
    if mode == "core":
        if res.get("patch"):
            raise BadResult("fold: patch only with after or replace")
        return {"mode": mode}
    return {"mode": mode, "patch": _ops(res.get("patch", []), ctx.get("seq"))}


def actions_answer(res, ctx):
    """The /hook/actions answer: {drop: [number], replace: {number: fields}, add: [action]} — the shape; the meaning is in edit_actions."""
    if not isinstance(res, dict) or set(res) - {"drop", "replace", "add"}:
        raise BadResult("actions: {drop, replace, add}")
    drop, repl, add = res.get("drop", []), res.get("replace", {}), res.get("add", [])
    if not (isinstance(drop, list) and all(type(i) is int for i in drop)):
        raise BadResult("actions: drop is action numbers")
    if not (isinstance(repl, dict) and all(isinstance(k, str) and re.fullmatch(r"[0-9]{1,6}", k) and isinstance(v, dict)
                                           for k, v in repl.items())):
        raise BadResult("actions: replace is number -> fields")
    if not (isinstance(add, list) and all(isinstance(a, dict) and isinstance(a.get("do"), str) for a in add)):
        raise BadResult("actions: add is a list of actions")
    out = {"drop": sorted(set(drop)), "replace": {str(int(k)): v for k, v in repl.items()}, "add": add}
    return {k: v for k, v in out.items() if v}


# what can be changed in a core action and what can be added: only what the host does — an action edit does not change state
REPLACE = {"type": {"text": str, "attachments": list}, "compact": {"text": str}, "respond": {"response": dict}}
FIELD = {"session": str, "text": str, "message": str, "attachments": list, "request_id": str, "response": dict}
ADD = {"type": ({"session", "text"}, {"message", "attachments"}),
       "respond": ({"session", "request_id", "response"}, set()),
       "compact": ({"session"}, {"text"}),
       **{d: ({"session"}, set()) for d in ("release", "kill", "interrupt", "end_tree", "recap")}}


def _typed(field, v):
    return isinstance(v, FIELD[field]) and (field != "attachments" or all(isinstance(x, str) for x in v))


def edit_actions(actions, edit, st):
    """The action list after the module's edit; a table violation (spec §5) — BadResult, the edit is not applied."""
    n, drop = len(actions), set(edit.get("drop") or [])
    if any(i < 0 or i >= n for i in drop):
        raise BadResult("actions: drop — number out of the list")
    out = [dict(a) for a in actions]
    for k, fields in (edit.get("replace") or {}).items():
        i = int(k)
        if i >= n or i in drop:
            raise BadResult(f"actions: replace {i} — no such action")
        allowed = REPLACE.get(out[i].get("do"), {})
        bad = [f for f, v in fields.items() if f not in allowed or not _typed(f, v)]
        if bad or not fields:
            raise BadResult(f"actions: {out[i].get('do')} cannot change {', '.join(bad) or 'nothing'}")
        out[i].update(fields)
    for a in edit.get("add") or []:
        do = a.get("do")
        if not isinstance(do, str) or do not in ADD:
            raise BadResult(f"actions: add {do} is not allowed — only {', '.join(sorted(ADD))}")
        need, may = ADD[do]
        keys = set(a) - {"do"}
        if need - keys or keys - need - may or not all(_typed(f, a[f]) for f in keys):
            raise BadResult(f"actions: add {do} — fields {', '.join(sorted(need))}"
                            + (f" (and {', '.join(sorted(may))})" if may else ""))
        if a["session"] not in st["sessions"]:
            raise BadResult(f"actions: add {do} — no session {a['session']}")
        if "message" in a and (st["messages"].get(a["message"]) or {}).get("session") != a["session"]:
            raise BadResult(f"actions: add type — {a['message']} is not a message of the session {a['session']}")
    return [a for i, a in enumerate(out) if i not in drop] + [dict(a) for a in edit.get("add") or []]


# --- folding a record (reducer.reduce: the same code in hubd and in all readers) --------------------------------------

def intervened(f):
    """A module's intervention in handling: after, replace (even without edits), reject or an action edit."""
    return isinstance(f, dict) and (f.get("mode") in ("after", "replace", "reject") or bool(f.get("actions")))


def author(ev):
    """The module whose record it is: an event with by or with an intervention in fold; None — an ordinary one."""
    by = ev.get("by")
    if isinstance(by, str) and by:
        return by
    f = ev.get("fold")
    return f.get("mod") if intervened(f) and isinstance(f.get("mod"), str) else None


def mark(st, name, ev):
    """A mark in the state: module name wrote to the core state (first/last are record numbers)."""
    cw = st.setdefault("core_written", {"format": FORMAT, "modules": {}})
    rec = cw["modules"].setdefault(name, {"first": ev.get("seq"), "since": ev.get("ts"), "count": 0})
    rec["last"] = ev.get("seq")
    rec["count"] = rec.get("count", 0) + 1
    rec.pop("forgot", None)   # wrote again — the warning is back on the page


def patch(st, ops):
    """Edits in order; one that did not apply (no parent, del without a key) is skipped. -> how many did not apply."""
    missed = 0
    for op in ops if isinstance(ops, list) else []:
        path = op.get("path") if isinstance(op, dict) else None
        if not (isinstance(path, list) and path and all(isinstance(k, str) for k in path)) \
                or path[0] in CLOSED or path[0].startswith("_"):
            missed += 1
            continue
        parent = st
        for k in path[:-1]:
            parent = parent.get(k) if isinstance(parent, dict) else None
        if not isinstance(parent, dict) or (op.get("op") == "del" and path[-1] not in parent):
            missed += 1
        elif op.get("op") == "set":
            from core.reducer import copy_state
            parent[path[-1]] = copy_state(op.get("value"))
        elif op.get("op") == "del":
            del parent[path[-1]]
        else:
            missed += 1
    return missed


def folded(st, ev):
    """The part of folding a record with fold or by (after the core's _apply, before the common tail): after/replace edits,
    the core_written mark; for core.rejected — the mark of a module's refusal (mod) and the count of core failures after the first mark."""
    if ev.get("type") == "core.rejected":
        d = ev.get("data") or {}
        if isinstance(d.get("mod"), str) and d["mod"]:
            mark(st, d["mod"], ev)
        elif "core_written" in st and forms.is_reason(forms.CORE_FAILURE, d.get("code"), d.get("reason")):
            st["core_written"]["crashes"] = st["core_written"].get("crashes", 0) + 1
        return
    f = ev.get("fold")
    if isinstance(f, dict) and f.get("mode") in ("after", "replace"):
        missed = patch(st, f.get("patch") or [])
        if missed:   # not so in hubd (it does not write such an edit); in a reader — after a rotation
            st["_patch_missed"] = st.get("_patch_missed", 0) + missed
    who = author(ev)
    if who:
        mark(st, who, ev)


def forget(st, data):
    """module.forget: the owner or the hub dismisses the warning on the page; the mark and the file remain."""
    from core import guards
    # a plugin or module process (channel plugin) may not: the shared from_hub_or_owner lets through any channel except a session
    if guards.from_owner_or_hub(st, data):
        raise Rejected(T("core.corewrite.forget_by_owner",
                         "a warning about a module is dismissed by the owner or by the hub on their word"))
    name = str(data.get("module") or "")
    rec = ((st.get("core_written") or {}).get("modules") or {}).get(name)
    if rec is None:
        raise Rejected(T("core.corewrite.forget_nothing",
                         "module {name} did not write to the core state — nothing to dismiss").format(name=name))
    rec["forgot"] = True


def module_marks(events):
    """{name: {count, first, last}} — module records in the journal (fold cross-checks, analysis of someone else's hub)."""
    out = {}
    for e in events:
        who = author(e) or ((e.get("data") or {}).get("mod") if e.get("type") == "core.rejected" else None)
        if who:
            rec = out.setdefault(who, {"count": 0, "first": e.get("seq")})
            rec["count"] += 1
            rec["last"] = e.get("seq")
    return out


# --- checks before the write (hubd) --------------------------------------------------------------------------------

def _states(machine):
    out = {machine["initial"]}
    for by in machine["transitions"].values():
        for frm, to in by.items():
            out.add(frm)
            out.update(t[0] for t in (to if isinstance(to, list) else [to]))
    return out


# keys the core creates on a session (session.requested) and a message (new_message): an edit does not remove them
SESSION_KEYS = ("name", "state", "cwd", "session_id", "mode", "ever_started", "restarts", "pid", "pane", "pending",
                "since", "kind", "project", "tools", "requests", "dirty", "closing", "role", "about", "icon",
                "news_seen")
MESSAGE_KEYS = ("id", "to", "session", "text", "origin", "state", "attempt", "deadline", "created", "since")


def entity_problems(before, after, ops):
    """Gross corruption in entities that patch touched: a state outside the machine, missing core keys, a name that is not the key."""
    from core.machines import CLOSING, DELEGATION, ENDED, MESSAGE, SESSION
    touched = {(op["path"][0], op["path"][1]) for op in ops if len(op["path"]) > 1
               and op["path"][0] in ("sessions", "messages", "delegations")}
    touched |= {(op["path"][0], k) for op in ops if len(op["path"]) == 1 and op["op"] == "set"
                and op["path"][0] in ("sessions", "messages", "delegations") and isinstance(op.get("value"), dict)
                for k in op["value"]}
    out = []
    one = lambda v, allowed: isinstance(v, str) and v in allowed  # noqa: E731 — a value from an edit: any JSON
    gone = set(before.get("sessions") or {}) - set(after.get("sessions") or {})
    if gone:   # a session was removed — live messages and assignments must not refer to it
        hang = [m.get("id") for m in (after.get("messages") or {}).values() if isinstance(m, dict)
                and m.get("session") in gone and m.get("state") in ("queued", "typing")]
        hang += [d.get("id") for d in (after.get("delegations") or {}).values() if isinstance(d, dict)
                 and d.get("session") in gone and d.get("state") not in ENDED]
        if hang:
            out.append(f"removed sessions {', '.join(sorted(gone))} are still referenced by "
                       f"{', '.join(map(str, hang[:5]))}")
    for box, key in sorted(touched):
        rec = (after.get(box) or {}).get(key)
        if rec is None:
            continue   # removed entirely — the core no longer sees it
        if not isinstance(rec, dict):
            out.append(f"{box}.{key} is not an object")
            continue
        old = (before.get(box) or {}).get(key) or {}
        machine, keys = {"sessions": (SESSION, SESSION_KEYS), "messages": (MESSAGE, MESSAGE_KEYS),
                         "delegations": (DELEGATION, ())}[box]
        if not one(rec.get("state"), _states(machine)):
            out.append(f"{box}.{key}: state {rec.get('state')!r} is not in the machine")
        need = [k for k in keys if (k in old if old else True) and k not in rec]
        if need:
            out.append(f"{box}.{key}: missing core keys {', '.join(need)}")
        if box == "sessions":
            if rec.get("name") != key:
                out.append(f"sessions.{key}: name {rec.get('name')!r} is not the key")
            if "closing" in rec and not one(rec["closing"], _states(CLOSING)):
                out.append(f"sessions.{key}: closing {rec['closing']!r} is not in the machine")
        if box == "messages" and rec.get("session") is not None and not one(rec.get("session"), after["sessions"]):
            out.append(f"messages.{key}: no session {rec.get('session')}")
    return out


def target(st, ev):
    """The event's session: for message.received the addressee by data.to (name, alias, "hub"), otherwise entity."""
    if ev.get("type") == "message.received":
        from core import identity
        from core.reducer import _hub
        to = str((ev.get("data") or {}).get("to") or "").strip()
        return identity.address(st, to) if to else _hub(st)
    return ev.get("entity") if isinstance(ev.get("entity"), str) else None


def part(st, ev, keys, name):
    """A narrow part of the state for /hook/fold (the manifest's fold_state)."""
    who = target(st, ev)
    out = {}
    for k in keys:
        if k == "session":
            out[k] = st["sessions"].get(who) if who else None
        elif k == "queue":
            out[k] = [m for m in st["messages"].values() if who and m.get("session") == who
                      and m.get("state") in ("queued", "typing")]
        elif k == "mods":
            out[k] = (st.get("mods") or {}).get(name)
        else:
            out[k] = st.get(k)
    return out


def _size(x):
    return len(json.dumps(x, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


# --- hubd ----------------------------------------------------------------------------------------------------------

CUT = "cut"   # the call cut the pass budget (less than half of its own was left) — a budget skip, not a module failure


class _Calls(modrun.Modules):
    """/hook/fold and /hook/actions calls: the budget is no longer than what is left of the pass; a failure goes to the queue
    (hubd writes module.error on the line after the event, not before it)."""

    def within(self, name, point, payload, left, ctx=None):
        spec = modrun.POINTS[point]
        run = self.ready(name)
        if run is None or point not in ((self.records() or {}).get(name) or {}).get("points", []):
            return None
        want = modrun.wants(self.state_dir).get(name) or {}
        own = min(spec.get("budget", modrun.BUDGET), (want.get("budget") or {}).get(point) or modrun.BUDGET * 1000)
        cut = left < own / 2   # less than half of its own budget — not waiting for the answer is not the module's fault
        if left <= 0.005:
            return CUT
        kind, value = modrun.post(run["port"], run["secret"], f"/hook/{point}", payload, min(own, left))
        if kind == "timeout" and cut:
            return CUT
        if not kind:
            try:
                return spec["check"](value, {"name": name, **(ctx or {})})
            except Exception as e:  # noqa: BLE001 — a module answer of any shape does not crash hubd: a module failure
                kind, value = "schema", str(e) if isinstance(e, BadResult) else f"{type(e).__name__}: {e}"
        self.fail(name, point, kind, value, run.get("launch"))
        return None


class CoreWrite:
    """A part of hubd: who intercepts which types, the calls, the verdict before the write, the pass budget. Without modules with fold and
    core.write it does nothing (on() is false): the write goes the old way, byte for byte."""

    def __init__(self, hub, mono=time.monotonic):
        self.hub, self.mono = hub, mono
        self.pending = []     # this record's module.error events are written after it
        self.calls = _Calls(hub.state_dir, records=lambda: hub.state.get("modules") or {},
                            emit=lambda t, e=None, d=None: self.pending.append((t, e, d)), clock=hub.clock)
        self.seen, self.hooks, self.writers = None, [], {}
        self.spent = {}       # module -> seconds in this pass
        self.window = {}      # module -> deque[(when, seconds)] over WINDOW
        self.slow = set()     # exceeded its share — skipped until it drops
        self.marked = set()   # modules whose line is already in the core-written file

    # what is loaded now
    def refresh(self):
        written = self.hub.modules.written
        if written is self.seen:
            return
        self.seen = written
        mans = {r["name"]: r["manifest"] for r in self.hub.modules.found}
        self.hooks, self.writers = [], {}
        for name, w in sorted((written or {}).items(), key=lambda x: (x[1].get("order", 0), x[0])):
            man = mans.get(name) or {}
            if CAP not in (w.get("capabilities") or []):
                continue
            self.writers[name] = man
            self.hooks += [(p, name) for p in man.get("fold") or []]

    def on(self):
        if not self.hub.modules.written:
            return False
        self.refresh()
        return bool(self.writers)

    def interceptor(self, ev):
        """The module intercepting this event, or None. A module's own event does not go to it."""
        kind = ev["type"]
        if not isinstance(kind, str) or kind.startswith(FORBIDDEN):
            return None
        return next((n for p, n in self.hooks if covers(p, kind) and n != ev.get("by")), None)

    def new_pass(self):
        self.spent = {}

    # inbox facts
    def admit(self, item):
        """An inbox fact with the field module -> (by, refusal): by — write with the module's label, refusal — the core.rejected reason."""
        name, kind = item.get("module"), item.get("type")
        if not (isinstance(name, str) and modules.NAME.fullmatch(name)):
            return None, "the module field is the module name"
        if not (isinstance(kind, str) and kind):
            return None, f"module {name}: type is a string"
        if kind.startswith(("core.", "module.")) or (kind.startswith("mod.") and not kind.startswith(f"mod.{name}.")):
            return None, f"module {name} may not write {kind}: only the core or another module writes it"
        if kind.startswith(f"mod.{name}."):
            return None, None   # the module's own state (core/modstate.py) is not a write to the core state: no by
        if self.on() and name in self.writers and self.calls.ready(name):
            return name, None
        if kind in ("owner.notify", "owner.notify_resolved", "message.received"):
            return None, None   # the shared module list (C2, C5) — as before
        return None, f"module {name} without {CAP} may not write: {kind}"

    # budget
    def _used(self, name, now):
        win = self.window.setdefault(name, deque())
        while win and win[0][0] < now - WINDOW:
            win.popleft()
        return sum(d for _, d in win)

    def spend(self, name, began):
        now = self.mono()
        took = max(0.0, now - began)
        self.spent[name] = self.spent.get(name, 0.0) + took
        self.window.setdefault(name, deque()).append((now, took))
        if name not in self.slow and self._used(name, now) > SHARE * WINDOW:
            self.slow.add(name)
            self.calls.fail(name, "fold", "slow", f"the module took more than {SHARE * WINDOW:g} s of hubd within "
                                                  f"{WINDOW:g} s — events go without it until it calms down")

    def left(self, name):
        return PASS - self.spent.get(name, 0.0)

    def skip(self, name):
        if self.hub.stopping:
            return "stopping"
        status = self.calls.status(name)
        if status == "suspended":
            return "suspended"
        if self.calls.ready(name) is None:
            return "not_ready"
        if name in self.slow:
            if self._used(name, self.mono()) > SHARE * WINDOW:
                return "budget"
            self.slow.discard(name)
        if self.left(name) <= 0.001:
            return "budget"
        return None

    # mark
    def ensure_mark(self, name, seq):
        """The module's line in the core-written file, with fsync before its first write: core code that does not
        understand such records will not start on the file even if state.json is lost."""
        if name in self.marked:
            return
        path = os.path.join(self.hub.state_dir, modules.CORE_WRITTEN)
        try:
            with open(path, encoding="utf-8") as f:
                cur = json.load(f)
        except (OSError, ValueError):
            cur = None
        if not (isinstance(cur, dict) and isinstance(cur.get("modules"), dict)):
            cur = {"format": FORMAT, "modules": {}}
        if name not in cur["modules"]:
            cur["format"] = max(FORMAT, cur.get("format") if isinstance(cur.get("format"), int) else FORMAT)
            cur["modules"][name] = {"first_seq": seq, "ts": round(self.hub.clock(), 3)}
            tmp = f"{path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            platforms.current.replace(tmp, path)
        self.marked.add(name)

    # write
    def flush(self):
        """This record's module.error events go on the next lines."""
        while self.pending:
            t, e, d = self.pending.pop(0)
            self.hub.record(t, e, d)

    def record(self, ev, mod, deny):
        """Writing a fact that concerns modules: an inbox refusal (deny), an event with by, an interception (mod). -> actions."""
        h = self.hub
        if deny is not None:
            rej = h.rejection(ev, deny)
            h.state, actions, _ = h.fold(rej)   # in place, with the spare: no copy of the whole state (Hub.fold)
            h.journal.write(ev, rej)
            return actions
        if ev.get("by"):
            self.ensure_mark(ev["by"], ev["seq"])
        if mod is None:
            return h.fold_write(ev)[0]
        actions = self.intercept(ev, mod)
        self.flush()
        return actions

    def intercept(self, ev, mod):
        h = self.hub
        why = self.skip(mod)
        if why:
            ev["fold"] = {"mod": mod, "skipped": why}
            return h.fold_write(ev)[0]
        man = self.writers.get(mod) or {}
        began = self.mono()
        p = part(h.state, ev, man.get("fold_state") or [], mod)
        if _size(p) > MAX_PART:
            self.spend(mod, began)
            ev["fold"] = {"mod": mod, "skipped": "part"}
            return h.fold_write(ev)[0]
        ans = self.calls.within(mod, "fold", {"api": version.MODULE_API, "event": ev, "part": p}, self.left(mod),
                                ctx={"seq": ev["seq"]})
        self.spend(mod, began)
        if ans is CUT:
            ev["fold"] = {"mod": mod, "skipped": "budget"}
        if ans is None or ans is CUT:
            return h.fold_write(ev)[0]
        fold = {"mod": mod, **ans}
        if ans["mode"] == "reject":
            ev["fold"] = fold
            self.ensure_mark(mod, ev["seq"])
            rej = h.rejection(ev, T("core.reducer.module_refused", "module {mod}: {reason}").format(
                mod=mod, reason=ans["reason"]), mod=mod)   # the same phrase as the fold's (core/reducer.py)
            h.state, actions, _ = h.fold(rej)
            h.journal.write(ev, rej)
            return actions
        # the fold with the module's edits goes in place (Hub.fold_try), the spare stays the state before the event — the
        # "before" of the checks: kept — the same event into the spare (fold_keep); dropped — the spare is the state again
        # (fold_undo), and the core folds the event itself. A copy of the whole state here cost tens of ms per event on a big hub
        trial = {**ev, "fold": {**fold}}   # one event for both halves (each folds its own copy): fold gets the action edit later
        new, actions, why = h.fold_try(trial)
        if why:   # fold_try has rolled the live state back to the spare
            began = self.mono()
            out, core_why = h.fold_write(ev)
            self.spend(mod, began)
            if not core_why:   # the fold with edits failed, the core accepts without them; if the core itself refused — no error
                self.calls.fail(mod, "fold", "rejected", f"the fold with edits was rejected: {why}")
            return out
        try:
            missed = new.pop("_patch_missed", 0)
            began = self.mono()
            edit = None
            if man.get("actions") is True:
                got = self.calls.within(mod, "actions", {"api": version.MODULE_API, "event": {**ev, "fold": fold},
                                                         "actions": actions}, self.left(mod))
                if got and got is not CUT:
                    try:
                        actions, edit = edit_actions(actions, got, new), got
                    except Exception as e:  # noqa: BLE001 — an edit of any shape does not crash hubd: core actions stay as they are
                        self.calls.fail(mod, "actions", "schema", str(e) if isinstance(e, BadResult)
                                        else f"{type(e).__name__}: {e}")
            problems = [f"edits that did not apply: {missed}"] if missed else []
            try:
                problems += entity_problems(h.spare, new, fold.get("patch") or [])
            except Exception as e:  # noqa: BLE001 — corruption the check could not parse is also a module failure
                problems.append(f"check: {type(e).__name__}: {e}")
            if _size({**fold, "actions": edit}) > MAX_FOLD:
                problems.append(f"the module's decision is larger than {MAX_FOLD >> 10} KB")
            self.spend(mod, began)
            if not problems:
                if edit:
                    fold["actions"] = edit
                if intervened(fold):
                    self.ensure_mark(mod, ev["seq"])
        except BaseException:
            h.fold_undo(ev)   # the trial fold is not left in the live state when the write does not happen
            raise
        if problems:
            h.fold_undo(ev)
            self.calls.fail(mod, "fold", "check", "; ".join(problems)[:500])
            began = self.mono()
            out = h.fold_write(ev)[0]
            self.spend(mod, began)
            return out
        h.fold_keep(trial)
        if intervened(fold) and fold["mode"] == "core":
            for st in (new, h.spare):
                mark(st, mod, ev)   # an intervention only by an action edit: readers set the same mark (folded)
        ev["fold"] = fold
        h.journal.write(ev)
        return actions


# --- display: hub modules, "About the hub", hub modules touched --------------------------------------------------------

def on_now(cfg, name):
    """The module is enabled and has consent for core.write — it can write to the core state now."""
    s = modules.settings(cfg)
    return name in (s.get("enabled") or []) and name in (s.get("core_write") or [])


def marks(st, cfg=None):
    """{name: {first, last, since, count, on, forgot}} — modules that wrote to the core state (page snapshot)."""
    out = {}
    for name, rec in ((st.get("core_written") or {}).get("modules") or {}).items():
        out[name] = {k: rec.get(k) for k in ("first", "last", "since", "count")}
        out[name].update(on=on_now(cfg or {}, name), forgot=bool(rec.get("forgot")))
    return out


def warnings(st, cfg=None):
    """Warning lines for hub modules and "About the hub": which module wrote to the core state, and whether the core failed afterwards."""
    cw = st.get("core_written") or {}
    out = []
    for name, rec in sorted(marks(st, cfg).items()):
        when = time.strftime("%d.%m %H:%M", time.localtime(rec["since"])) if rec.get("since") else "?"
        out.append(i18n.t("core.corewrite.warn_wrote" if rec["on"] else "core.corewrite.warn_wrote_off",
                          name=name, when=when, count=rec.get("count") or 0))
    if out and cw.get("crashes"):
        out.append(i18n.t("core.corewrite.warn_crashes", count=cw["crashes"]))
    if out:
        out.append(i18n.t("core.corewrite.warn_fold"))
    return out


def _journal_files(state_dir):
    from core import journal
    return journal.archives(state_dir) + [os.path.join(state_dir, "journal.jsonl")]


def touched(state_dir, name, st, days=7, now=None):
    """What module name edited over days days (journal and archives): the entities and where they are now; what the core
    will not move by itself (a message with held and no hint to the hub, a busy session without a process) is flagged."""
    now = time.time() if now is None else now
    since = now - days * 86400
    needle = (f'"by":"{name}"'.encode(), f'"mod":"{name}"'.encode())
    seen, count = {}, 0
    for path in _journal_files(state_dir):
        try:
            if os.path.getmtime(path) < since:
                continue
            fh = open(path, "rb")
        except OSError:
            continue
        with fh:
            for ln in fh:
                if not any(n in ln for n in needle):
                    continue
                try:
                    e = json.loads(ln)
                except ValueError:
                    continue
                if e.get("ts", 0) < since or author(e) != name:
                    continue
                count += 1
                f = e.get("fold") or {}
                for op in f.get("patch") or []:
                    p = op.get("path") or []
                    if len(p) > 1 and p[0] in ("sessions", "messages", "delegations"):
                        seen.setdefault((p[0], p[1]), e["seq"])
                if e.get("by") and isinstance(e.get("entity"), str):
                    seen.setdefault(("sessions" if e["entity"] in st.get("sessions", {}) else "messages",
                                     e["entity"]), e["seq"])
    lines = [i18n.t("core.corewrite.touched_head", name=name, days=days, count=count, entities=len(seen))]
    for (box, key), seq in sorted(seen.items(), key=lambda x: x[1]):
        rec = (st.get(box) or {}).get(key)
        at = {"box": box, "entity": key, "seq": seq}
        if rec is None:
            lines.append("  " + i18n.t("core.corewrite.seen_gone", **at))
            continue
        line = "core.corewrite.seen_line"
        if box == "messages" and rec.get("held") and rec["held"] not in st.get("messages", {}):
            line = "core.corewrite.seen_held"
        if box == "sessions" and rec.get("state") == "busy" and not rec.get("pid"):
            line = "core.corewrite.seen_busy"
        lines.append("  " + i18n.t(line, state=rec.get("state"), **at))
    return lines
