"""The "who wrote to whom" flow for the animation on the page (owner request 2026-10-02: an animation over the journal
of who wrote to whom, in a modern style of animated dots and signals between them). A signal comes from each new
session message (from whom — by the origin label), from a turn result (session → whoever started the turn: the owner,
a session, the schedule, the core) and from a reply to the network (operator → another hub)."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import tempfile
import unittest

from core import views
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
head = lang.head   # the label the earlier core put at a turn's start: [hub:mN <from> <who>]


class Flow(unittest.TestCase):
    def setUp(self):
        self.r, self.seq = views.Reader(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/d", session_id="s-d", role="agent", **OWNER)
        for n in ("hub", "devops"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.r.feed({"seq": self.seq, "ts": 1000 + self.seq, "type": type, "entity": entity, "data": data})

    def pairs(self):
        return [(f["from"], f["to"], f["kind"]) for f in self.r.flow]

    def test_who_wrote_to_whom(self):
        self.ev("message.received", None, text="deploy the service", **OWNER)                       # owner → hub
        self.ev("delegation.created", None, session="devops", context="a task", channel="session", owner=False,
                sender="hub")                                                                     # hub → devops
        self.ev("message.received", None, to="hub", text="ready", channel="session", owner=False,
                sender="devops")                                                                  # devops → hub
        self.ev("turn.started", "hub", marker=None)
        self.ev("turn.result", "hub", text="The service is deployed.")                                   # hub → owner
        self.ev("turn.result", "hub", text="—")                                                   # a silent result — no signal
        self.assertEqual(self.pairs(), [("owner", "hub", "message"), ("hub", "devops", "task"),
                                        ("devops", "hub", "message"), ("hub", "owner", "result")])
        self.assertTrue(all(f["seq"] and f["ts"] for f in self.r.flow))

    def test_a_command_handed_to_a_session_wakes_nobody(self):
        # 2026-10-24: a command can name a session (whose it is); the clock runs it without a session — no "schedule → session" arrow
        import json
        from core import flow
        lines = [json.dumps({"seq": n, "ts": 1000 + n, "type": t, "entity": who, "data": d}, separators=(",", ":")).encode()
                 for n, (t, who, d) in enumerate([
                     ("schedule.set", "mon", {"command": "true", "session": "devops"}),
                     ("schedule.due", "mon", {"slot": 1}),
                     ("schedule.set", "job", {"prompt": "p", "session": "devops"}),
                     ("schedule.due", "job", {"slot": 1}),
                     ("schedule.set", "job", {"command": "true", "session": "devops"}),   # it stopped being the session's job
                     ("schedule.due", "job", {"slot": 2})], 1)]
        self.assertEqual([(f["from"], f["to"], f["kind"]) for f in flow.signals(lines)], [("schedule", "devops", "message")])

    def test_removed_session_leaves_the_flow(self):
        # a session must disappear from the flow for the time when it no longer existed
        self.ev("session.requested", "w", cwd="/w", session_id="s-w", **OWNER)
        self.ev("session.started", "w")
        self.ev("session.remove_requested", "w", **OWNER)                    # a live session cannot be removed — a rejection, not "gone"
        self.ev("session.ended", "w", reason="prompt_input_exit")
        self.ev("session.remove_requested", "w", **OWNER)
        self.assertEqual([(f["from"], f["kind"]) for f in self.r.flow if f["kind"] == "gone"], [("w", "gone")])

    def test_since(self):
        self.ev("message.received", None, text="one", **OWNER)
        first = self.r.flow[-1]["seq"]
        self.ev("message.received", None, text="two", **OWNER)
        self.assertEqual([f["seq"] for f in self.r.flow_since(first)], [self.seq])


# A turn result goes to whoever started the turn ("→ You" was drawn for every result of any session — 1254
# in a day, of which ~180 were for the owner). Who started it — the "from X" part of the label `[hub:mN …]` on the
# turn's first message, which the session itself also sees.
TURNS = [
    ("session.requested", "hub", {"kind": "hub", "cwd": "/h", "session_id": "s-h", **OWNER}),
    ("session.requested", "devops", {"role": "agent", "cwd": "/d", "session_id": "s-d", **OWNER}),
    ("session.requested", "maps", {"cwd": "/m", "session_id": "s-m", **OWNER}),
    ("session.started", "hub", {}), ("session.started", "devops", {}), ("session.started", "maps", {}),
    # hub: any of its results goes to the owner (its conversation with the owner), including on a session message
    ("turn.started", "hub", {"marker": "m90", "prompt_head": f"{head('m90', 'devops')} ready"}),
    ("turn.result", "hub", {"text": "devops is done"}),
    # a turn on a session message goes to that session
    ("turn.started", "devops", {"marker": "m91", "prompt_head": f"{head('m91', 'maps')} look at the logs"}),
    ("turn.result", "devops", {"text": "the logs are clean"}),
    # a task to an executor from a session (with the "reply to task" suffix) goes to the one who assigned it
    ("turn.started", "maps", {"marker": "m92", "prompt_head": f"{head('m92', 'devops')[:-1]}, reply to task d5] done"}),
    ("turn.result", "maps", {"text": "accepted"}),
    # an owner's word inserted mid-turn on a session message makes the turn the owner's
    ("turn.started", "devops", {"marker": "m93", "prompt_head": f"{head('m93', 'maps')} more"}),
    ("turn.started", "devops", {"marker": "m94", "prompt_head": f"{head('m94', kind='owner')} and this too"}),
    ("turn.result", "devops", {"text": "did both"}),
    # background work finished (a turn without a label) — a continuation of the previous turn: to the owner
    ("turn.started", "devops", {"marker": None, "prompt_head": "<task-notification>…"}),
    ("turn.result", "devops", {"text": "the build passed"}),
    # a reply to a card, an owner's task (no signature), the schedule, the core, a module
    ("turn.started", "maps", {"marker": "m95", "prompt_head": f"{head('m95', kind='owner')} The owner's answer to q7: "
                                                              f"{lang.word('words', 'yes', 1)}"}),
    ("turn.result", "maps", {"text": "on it"}),
    ("turn.started", "maps", {"marker": "m96", "prompt_head": f"[hub:m96] {lang.legacy('record_task')} d8: collect the report"}),
    ("turn.result", "maps", {"text": "the report is ready"}),
    ("turn.started", "maps", {"marker": "m97", "prompt_head": f"{head('m97', kind='schedule')} [⏰ schedule tick] check"}),
    ("turn.result", "maps", {"text": "nothing new"}),
    ("turn.started", "maps", {"marker": "m98x5", "prompt_head": f"{head('m98x5', kind='core')} Executor x handed in the task"}),
    ("turn.result", "maps", {"text": "noted"}),
    ("turn.started", "maps", {"marker": "m99", "prompt_head": f"{head('m99', lang.legacy('who')['module'].format(name='digest'))}"
                                                             " summary"}),
    ("turn.result", "maps", {"text": "read"}),
    ("turn.result", "maps", {"text": "—"}),   # a silent result is not a signal
    # an owner's word merged with a session message into one turn (also) — the owner's turn
    ("turn.started", "devops", {"marker": "m100", "also": ["m101"],
                                "prompt_head": f"{head('m100', 'maps')} take a look\n{head('m101', kind='owner')} and this"}),
    ("turn.result", "devops", {"text": "looked"}),
    # the owner's turn was cut off by the process exiting (no result) — the session's next turn is its own, not a continuation
    ("turn.started", "devops", {"marker": "m102", "prompt_head": f"{head('m102', kind='owner')} assemble"}),
    ("session.exited", "devops", {}), ("session.started", "devops", {}),
    ("turn.started", "devops", {"marker": "m103", "prompt_head": f"{head('m103', 'maps')} how is it going"}),
    ("turn.result", "devops", {"text": "assembling"}),
    # the owner's turn was interrupted by the owner (a failure result) — the next turn is also its own
    ("turn.started", "devops", {"marker": "m104", "prompt_head": f"{head('m104', kind='owner')} stop"}),
    ("session.interrupt_requested", "devops", OWNER),
    ("turn.result", "devops", {"text": "Interrupted", "is_error": True}),
    ("turn.started", "devops", {"marker": "m105", "prompt_head": f"{head('m105', 'maps')} once more"}),
    ("turn.result", "devops", {"text": "did it"}),
    # the merged line is cut at the 200th character in the middle of a signature — not an owner's word: the turn follows the first message
    ("turn.started", "maps", {"marker": "m106", "also": ["m107"],
                              "prompt_head": f"{head('m106', 'devops')} check\n[hub:m107"}),
    ("turn.result", "maps", {"text": "checked"}),
]
EXPECTED = [("hub", "owner"), ("devops", "maps"), ("maps", "devops"), ("devops", "owner"), ("devops", "owner"),
            ("maps", "owner"), ("maps", "owner"), ("maps", "schedule"), ("maps", "core"), ("maps", "core"),
            ("devops", "owner"), ("devops", "maps"), ("devops", "maps"), ("maps", "devops")]


class ResultGoesToWhoStartedTheTurn(unittest.TestCase):
    def test_live_flow(self):
        r = views.Reader()
        for seq, (type, entity, data) in enumerate(TURNS, 1):
            r.feed({"seq": seq, "ts": 1000 + seq, "type": type, "entity": entity, "data": data})
        self.assertEqual([(f["from"], f["to"]) for f in r.flow if f["kind"] == "result"], EXPECTED)

    def test_history_draws_the_same(self):
        import json
        import os
        import tempfile
        from core import flow
        d = tempfile.mkdtemp(prefix="flow-")
        with open(os.path.join(d, "journal.jsonl"), "w") as f:
            for seq, (type, entity, data) in enumerate(TURNS, 1):
                f.write(json.dumps({"seq": seq, "ts": 1000 + seq, "type": type, "entity": entity, "data": data},
                                   ensure_ascii=False, separators=(",", ":")) + "\n")
        self.assertEqual([(s["from"], s["to"]) for s in flow.history(d, 0) if s["kind"] == "result"], EXPECTED)

    def test_notifications_do_not_follow_the_flow(self):
        # "new" on a session (page, Telegram) follows the fold rule (owner_out), "Flow" does not change it: a turn result
        # on a session message stays "new", although the arrow goes to that session
        r = views.Reader()
        for seq, (type, entity, data) in enumerate(TURNS[:10], 1):
            r.feed({"seq": seq, "ts": 1000 + seq, "type": type, "entity": entity, "data": data})
        self.assertEqual(r.state["sessions"]["devops"].get("owner_out"), 10)


class History(unittest.TestCase):
    def test_archive_and_current_journal(self):
        import json
        import os
        import tempfile
        from core import flow
        d = tempfile.mkdtemp(prefix="flow-")
        os.makedirs(os.path.join(d, "journal-archive"))

        def line(seq, type, entity=None, **data):
            return json.dumps({"seq": seq, "ts": 1000 + seq, "type": type, "entity": entity, "data": data},
                              ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(os.path.join(d, "journal-archive", "journal-1-5.jsonl"), "w") as f:
            f.write(line(1, "session.requested", "hub-core", kind="hub"))
            f.write(line(2, "message.received", None, text="x", channel="web", owner=True))
            f.write(line(3, "delegation.created", None, session="devops", channel="session", sender="hub-core"))
            f.write(line(4, "turn.started", "devops", marker="m3", prompt_head=f"{head('m3', 'hub-core')} a task"))
            f.write(line(5, "turn.result", "devops", text="ok"))
        with open(os.path.join(d, "journal.jsonl"), "w") as f:   # rotation starts the journal with a snapshot: who the hub is
            f.write(line(5, "core.snapshot", None, why="rotate-start",
                         state={"sessions": {"hub-core": {"kind": "hub"}}}))
            f.write(line(5, "turn.result", "devops", text="ok"))          # the tail of the old journal — not twice
            f.write(line(6, "message.received", None, to="hub", text="y", channel="session", sender="devops"))
            # a module and a former plugin process (a bare name) — their own nodes, as live, not the owner
            f.write(line(7, "message.received", None, to="devops", text="z", channel="plugin", sender="mod:fake"))
            f.write(line(8, "message.received", None, to="devops", text="z", channel="plugin", sender="desk"))
        got = [(s["from"], s["to"]) for s in flow.history(d, 0)]
        self.assertEqual(got, [("owner", "hub-core"), ("hub-core", "devops"), ("devops", "hub-core"),
                               ("devops", "hub-core"), ("module fake", "devops"), ("plugin desk", "devops")])
        self.assertEqual(len(flow.history(d, 1004)), 4)

    def test_history_knows_when_a_session_was_removed(self):
        import json
        import os
        import tempfile
        from core import flow
        d = tempfile.mkdtemp(prefix="flow-")

        def line(seq, ev_type, entity=None, **data):
            return json.dumps({"seq": seq, "ts": 1000 + seq, "type": ev_type, "entity": entity, "data": data},
                              ensure_ascii=False, separators=(",", ":")) + "\n"
        with open(os.path.join(d, "journal.jsonl"), "w") as f:
            f.write(line(1, "session.requested", "hub", kind="hub"))
            f.write(line(2, "delegation.created", None, session="w", channel="session", sender="hub"))
            f.write(line(3, "session.remove_requested", "busy-one", channel="cli", owner=True))
            f.write(line(4, "core.rejected", "busy-one", of=3, type="session.remove_requested", reason="live"))
            f.write(line(5, "session.remove_requested", "w", channel="cli", owner=True))
        got = [(s["from"], s["to"], s["kind"], s["ts"]) for s in flow.history(d, 0)]
        self.assertEqual(got, [("hub", "w", "task", 1002), ("w", "w", "gone", 1005)])   # a rejection — not "gone"

    def test_rotated_tails_come_once_and_old_archives_keep_no_signals(self):
        # 2026-10-15: rotation repeats the session tails in every following journal — the "Flow" replay showed them
        # several times (33k signals a week, 12k distinct), and the page held the signals of all archives
        import json
        import os
        import tempfile
        import time
        from core import flow
        d = tempfile.mkdtemp(prefix="flow-")
        arch = os.path.join(d, "journal-archive")
        os.makedirs(arch)
        now = time.time()

        def write(path, events, age=None):
            with open(path, "w") as f:
                for seq, ev_type, entity, data in events:
                    ts = now - (age or 0) - 100 + seq
                    f.write(json.dumps({"seq": seq, "ts": ts, "type": ev_type, "entity": entity, "data": data},
                                       ensure_ascii=False, separators=(",", ":")) + "\n")
            if age:
                os.utime(path, (now - age, now - age))
        start = {"why": "rotate-start", "state": {"sessions": {"hub": {"kind": "hub"}}}}
        say = lambda seq, who: (seq, "message.received", None,  # noqa: E731
                                {"to": "hub", "text": "x", "channel": "session", "sender": who})
        write(os.path.join(arch, "journal-1-3.jsonl"), [(1, "session.requested", "hub", {"kind": "hub"}),
                                                        say(2, "w0"), say(3, "w0")], age=10 * 86400)
        write(os.path.join(arch, "journal-3-6.jsonl"), [(3, "core.snapshot", None, start), say(3, "w0"),
                                                        say(4, "w1"), say(5, "w1"), say(6, "w1")])
        write(os.path.join(arch, "journal-3-9.jsonl"), [(3, "core.snapshot", None, start), say(3, "w0"),
                                                        say(5, "w1"), say(7, "w2"), say(8, "w2"), say(9, "w2")])
        write(os.path.join(d, "journal.jsonl"), [(8, "core.snapshot", None, start), say(9, "w2"), say(10, "w3")])
        got = [s["seq"] for s in flow.history(d, now - 3600)]
        self.assertEqual(got, [4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(flow._archive[os.path.join(arch, "journal-1-3.jsonl")][2], [])   # older than the window: no signals kept
        self.assertEqual([s["seq"] for s in flow.history(d, 0)], [2, 3, 4, 5, 6, 7, 8, 9, 10])


    def week(self):
        """Archives as produced by rotation: each starts with a snapshot (who the hub is, whose jobs). Older than a week —
        a schedule job was created, in the last hour — it fired."""
        import json
        import os
        import tempfile
        import time
        from core import flow
        flow._archive.clear()
        d = tempfile.mkdtemp(prefix="flow-")
        arch = os.path.join(d, "journal-archive")
        os.makedirs(arch)
        now = time.time()

        def write(name, events, age, jobs=None):
            path = os.path.join(arch, name)
            start = int(name.split("-")[1]) - 1
            state = {"sessions": {"hub-core": {"kind": "hub"}}, "schedule": jobs or {}}
            events = [(start, "core.snapshot", None, {"why": "rotate-start", "state": state})] + events
            with open(path, "w") as f:
                for seq, ev_type, entity, data in events:
                    f.write(json.dumps({"seq": seq, "ts": now - age - 100 + seq, "type": ev_type, "entity": entity,
                                        "data": data}, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.utime(path, (now - age, now - age))
            return path
        say = lambda seq, who: (seq, "message.received", None,  # noqa: E731
                                {"to": "hub", "text": "x", "channel": "session", "sender": who})
        job = {"j1": {"session": "w9"}}
        old = write("journal-1-2.jsonl", [(1, "schedule.set", "j1", {"session": "w9"}), say(2, "w0")], 10 * 86400)
        days = write("journal-3-4.jsonl", [say(4, "w1")], 3 * 86400, job)
        hours = write("journal-5-6.jsonl", [say(5, "w2"), (6, "schedule.due", "j1", {})], 3 * 3600, job)
        recent = write("journal-7-8.jsonl", [say(7, "w4"), say(8, "w5")], 600, job)
        return d, now, (old, days, hours, recent)

    def test_switching_hours_does_not_reread_the_archive(self):
        # "Flow" 1 h / 6 h / a day: an archive within the week was evicted by the request window and re-read on every switch
        from unittest import mock
        from core import flow
        d, now, (_, _, hours, _) = self.week()
        read, real = [], flow.signals

        def spy(lines, ctx=None, **kw):
            read.append(lines.name)
            return real(lines, ctx, **kw)
        with mock.patch.object(flow, "signals", spy):
            for h in (1, 6, 1, 6, 24):
                flow.history(d, now - h * 3600, now=now)
        self.assertEqual(read.count(hours), 1)

    def test_archives_long_before_the_window_are_not_read(self):
        # months of archives (tens of GB): the week is read from LEAD archives before it, not from the first archive;
        # the cache key does not nest the keys of all archives before
        import json
        import os
        from unittest import mock
        from core import flow
        d, now, (old, days, hours, recent) = self.week()
        arch = os.path.dirname(old)
        older = []
        for n in range(2):   # last numbers 0 and 1: listed before journal-1-2
            path = os.path.join(arch, f"journal-0-{n}.jsonl")
            with open(path, "w") as f:
                f.write(json.dumps({"seq": 0, "ts": now - 30 * 86400, "type": "message.received", "entity": None,
                                    "data": {"to": "hub", "text": "x", "channel": "session", "sender": "w8"}}) + "\n")
            os.utime(path, (now - (40 - n) * 86400, now - (40 - n) * 86400))
            older.append(path)
        self.assertEqual(flow.journal.archives(d)[:3], older + [old])
        read, real = [], flow.signals

        def spy(lines, ctx=None, **kw):
            read.append(lines.name)
            return real(lines, ctx, **kw)
        with mock.patch.object(flow, "signals", spy):
            got = [(s["seq"], s["to"]) for s in flow.history(d, now - flow.WINDOW, now=now)]
        self.assertEqual(got, [(4, "hub-core"), (5, "hub-core"), (6, "w9"), (7, "hub-core"), (8, "hub-core")])
        self.assertEqual(read, older[-1:] + [old, days, hours, recent])   # LEAD = 2 before the first in the window
        self.assertEqual(set(flow._archive), {older[-1], old, days, hours, recent})
        self.assertTrue(all(not isinstance(got[0][1], tuple) for got in flow._archive.values()))

    def test_an_hour_then_a_week_is_the_cold_week(self):
        # each archive starts from an empty ctx (who the hub is and whose jobs — from the snapshot at its start) and is
        # cached under one path: whatever filled the cache, the week is the same as from scratch
        from core import flow
        d, now, paths = self.week()
        week = now - flow.WINDOW
        cold = [(s["seq"], s["to"]) for s in flow.history(d, week, now=now)]
        self.assertEqual(cold, [(4, "hub-core"), (5, "hub-core"), (6, "w9"), (7, "hub-core"), (8, "hub-core")])
        hour = [(s["seq"], s["to"]) for s in flow.history(d, now - 3600, now=now)]
        for first in (now - 3600, 0):
            flow._archive.clear()
            flow.history(d, first, now=now)
            self.assertEqual([(s["seq"], s["to"]) for s in flow.history(d, week, now=now)], cold, first)
            self.assertEqual([(s["seq"], s["to"]) for s in flow.history(d, now - 3600, now=now)], hour, first)
            self.assertLessEqual(set(flow._archive), set(paths))   # the key is the archive path

    def test_archive_gone_while_reading(self):
        # an archive was removed between the search and the read — do not crash; the mtime of each archive once per request
        import os
        from unittest import mock
        from core import flow
        d, now, paths = self.week()
        gone = os.path.join(os.path.dirname(paths[0]), "journal-9-9.jsonl")
        real_archives, real_mtime, asked = flow.journal.archives, os.path.getmtime, []

        def mtime(p):
            asked.append(p)
            return real_mtime(p)
        with mock.patch.object(flow.journal, "archives", lambda state_dir: real_archives(state_dir) + [gone]), \
                mock.patch.object(flow.os.path, "getmtime", mtime):
            got = [s["seq"] for s in flow.history(d, now - flow.WINDOW, now=now)]
        self.assertEqual(got, [4, 5, 6, 7, 8])
        self.assertEqual(sorted(asked), sorted(paths + (gone,)))

    def test_eviction_tolerates_another_thread(self):
        # the cache is shared by the page's threads: an archive evicted by a neighbouring request is not a KeyError
        from unittest import mock
        from core import flow
        d, now, (old, *_) = self.week()

        class Racy(dict):
            def __iter__(self):   # a neighbour evicted it while this request was collecting keys
                keys = list(super().__iter__())
                self.clear()
                return iter(keys)
        with mock.patch.object(flow, "_archive", Racy({old: (0, 0, [])})):
            flow.history(d, now - 3600, now=now)

    def test_cache_is_walked_over_a_copy_of_its_keys(self):
        # the cache is shared by the page's threads: a neighbouring request added an archive while this one walked the
        # cache — "dictionary changed size during iteration" crashed "Flow"; the walk goes over a list of keys
        import os
        from unittest import mock
        from core import flow
        d, now, paths = self.week()
        flow.history(d, now - flow.WINDOW, now=now)    # the cache is full
        real = os.path.getmtime

        class Stamp(float):
            def __lt__(self, other):   # comparing mtime with the window, mid-walk of the cache: a neighbour added its archive
                flow._archive.setdefault(os.path.join(d, "journal-archive", "journal-90-99.jsonl"), (0, 0, []))
                return float(self) < other
        with mock.patch.object(flow.os.path, "getmtime", lambda p: Stamp(real(p))):
            got = [s["seq"] for s in flow.history(d, now - flow.WINDOW, now=now)]
        self.assertEqual(got, [4, 5, 6, 7, 8])

    def test_only_files_named_by_rotation_are_archives(self):
        # a copy of an archive under another name in its folder is not an archive: "Flow" is not doubled, and the cache does not depend on it
        import os
        import shutil
        from core import flow
        d, now, paths = self.week()
        cold = [(s["seq"], s["to"]) for s in flow.history(d, now - flow.WINDOW, now=now)]
        for p in paths:
            shutil.copy2(p, p[:-len(".jsonl")] + " copy.jsonl")
        flow._archive.clear()
        self.assertEqual([(s["seq"], s["to"]) for s in flow.history(d, now - flow.WINDOW, now=now)], cold)
        self.assertLessEqual(set(flow._archive), set(paths))


class CurrentJournal(unittest.TestCase):
    """The current journal — a cache of signals by read position: a request reads only what is new (on a hub with a
    journal of a hundred MB every "Flow" request parsed it anew — tens of seconds under load), and the signals are the
    same as with a full read."""

    def setUp(self):
        import time
        from core import flow
        flow._archive.clear()
        flow._current.clear()
        self.addCleanup(flow._current.clear)
        self.d = tempfile.mkdtemp(prefix="flow-cur-")
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self.path = os.path.join(self.d, "journal.jsonl")
        self.now, self.seq = time.time(), 0
        self.add(("core.snapshot", None, {"why": "rotate-start", "state": {"sessions": {"hub-core": {"kind": "hub"}},
                                                                          "schedule": {"j1": {"session": "w9"}}}}))

    def line(self, ev_type, entity, data):
        self.seq += 1
        return json.dumps({"seq": self.seq, "ts": self.now - 1000 + self.seq, "type": ev_type, "entity": entity,
                           "data": data}, ensure_ascii=False, separators=(",", ":")) + "\n"

    def add(self, *events, raw=""):
        with open(self.path, "a") as f:
            f.write("".join(self.line(*e) for e in events) + raw)

    @staticmethod
    def say(who, to="hub"):
        return ("message.received", None, {"to": to, "text": "x", "channel": "session", "sender": who})

    def whole(self, since=0, path=None):
        """As before the cache: the whole current journal in one read from an empty ctx."""
        from core import flow
        with open(path or self.path, "rb") as f:
            return [s for s in flow.signals(f) if s["ts"] >= since]

    def test_reading_in_parts_gives_what_reading_whole_gives(self):
        from core import flow
        parts = [
            [self.say("w1"), ("turn.started", "w1", {"marker": "m2", "prompt_head": head("m2", "hub-core")})],
            [("turn.result", "w1", {"text": "ready"}), ("schedule.due", "j1", {"exit": 1}), self.say("w2")],
            # a core rejection of a message from the previous portion: its signal is removed from what was already read too
            [("core.rejected", None, {"type": "message.received", "of": 6}), self.say("w3", to="w1")],
            [("session.remove_requested", "w2", {}), ("delegation.created", None,
                                                       {"session": "w4", "sender": "hub-core", "channel": "session"})],
        ]
        for part in parts:
            self.add(*part)
            self.assertEqual(flow.history(self.d, 0, now=self.now), self.whole())
        self.assertNotIn(6, [s["seq"] for s in flow.history(self.d, 0, now=self.now)])
        since = self.now - 1000 + 5
        self.assertEqual(flow.history(self.d, since, now=self.now), self.whole(since))

    def test_second_request_does_not_parse_read_lines_again(self):
        from unittest import mock
        from core import flow
        self.add(self.say("w1"), self.say("w2"))
        fed, real = [], flow.signals

        def spy(lines, ctx=None):
            lines = list(lines)
            fed.append(len(lines))
            return real(lines, ctx)
        with mock.patch.object(flow, "signals", spy):
            first = flow.history(self.d, 0, now=self.now)
            self.assertEqual(flow.history(self.d, 0, now=self.now), first)
            self.add(self.say("w3"))
            got = flow.history(self.d, 0, now=self.now)
        self.assertEqual(fed, [3, 0, 1])
        self.assertEqual([s["from"] for s in got], ["w1", "w2", "w3"])

    def test_half_written_line_is_read_when_it_is_whole(self):
        from core import flow
        self.add(self.say("w1"))
        tail = self.line(*self.say("w2"))
        with open(self.path, "a") as f:
            f.write(tail[:20])
        self.assertEqual([s["from"] for s in flow.history(self.d, 0, now=self.now)], ["w1"])
        with open(self.path, "a") as f:
            f.write(tail[20:])
        self.assertEqual([s["from"] for s in flow.history(self.d, 0, now=self.now)], ["w1", "w2"])

    def test_rotation_reads_the_new_journal_from_its_start(self):
        from core import flow
        self.add(self.say("w1"), self.say("w2"))
        self.assertEqual(len(flow.history(self.d, 0, now=self.now)), 2)
        os.replace(self.path, self.path + ".old")   # a new journal file (another inode), shorter than what was read
        self.add(self.say("w5"))
        self.assertEqual([s["from"] for s in flow.history(self.d, 0, now=self.now)], ["w5"])
        with open(self.path, "w"):   # the same file was shortened (the core cut off a broken tail) — read anew
            pass
        self.add(self.say("w6"))
        self.assertEqual([s["from"] for s in flow.history(self.d, 0, now=self.now)], ["w6"])

    def test_rejection_in_the_same_part_and_the_memory_of_the_read_tail(self):
        from core import flow
        self.add(self.say("w1"), self.say("w2"), ("core.rejected", None, {"type": "message.received", "of": 2}))
        self.assertEqual([s["from"] for s in flow.history(self.d, 0, now=self.now)], ["w2"])
        self.assertEqual(flow.history(self.d, 0, now=self.now), self.whole())
        self.assertLessEqual(len(flow._current["head"]), flow.PROBE)   # a snapshot line is tens of MB: we do not keep it whole

    def test_last_line_replaced_by_another_of_the_same_size_is_noticed(self):
        from core import flow
        self.add(self.say("w1"), self.say("w2"))
        self.assertEqual([s["from"] for s in flow.history(self.d, 0, now=self.now)], ["w1", "w2"])
        with open(self.path, "rb") as f:
            raw = f.read()
        cut = raw.rindex(b"\n", 0, len(raw) - 1) + 1
        self.seq -= 1   # the core crashed on a write, came back, the number is the same — the event is different (and so is the time)
        self.now += 1
        with open(self.path, "r+b") as f:
            f.truncate(cut)
        self.add(self.say("w3"), self.say("w4"))
        self.assertEqual([s["from"] for s in flow.history(self.d, 0, now=self.now)], ["w1", "w3", "w4"])

    def test_other_state_dir_is_another_journal(self):
        from core import flow
        self.add(self.say("w1"))
        other = tempfile.mkdtemp(prefix="flow-cur-")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        shutil.copyfile(self.path, os.path.join(other, "journal.jsonl"))   # the same bytes, another folder
        self.assertEqual(flow.history(other, 0, now=self.now), self.whole(path=os.path.join(other, "journal.jsonl")))
        self.assertEqual(flow.history(self.d, 0, now=self.now), self.whole())
        self.assertEqual(flow._current["key"][0], self.path)

    def test_parse_failure_in_the_middle_reads_again_from_the_start(self):
        from unittest import mock
        from core import flow
        self.add(self.say("w1"), ("turn.started", "w1", {"marker": "m2", "prompt_head": head("m2", "hub-core")}))
        flow.history(self.d, 0, now=self.now)
        self.add(("turn.result", "w1", {"text": "ready"}),
                 ("turn.started", "w1", {"marker": "m3", "prompt_head": head("m3", "w2")}))
        real = flow.signals

        def broken(lines, ctx=None):
            real(lines, ctx)   # ctx is already touched: the next turn of w1 was started by session w2 — the previous turn's result would go to it
            raise RuntimeError("the parse got stuck")
        with mock.patch.object(flow, "signals", broken):
            with self.assertRaises(RuntimeError):
                flow.history(self.d, 0, now=self.now)
        self.assertEqual(flow.history(self.d, 0, now=self.now), self.whole())

if __name__ == "__main__":
    unittest.main()
