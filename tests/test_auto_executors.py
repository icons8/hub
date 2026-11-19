"""A busy specialist. A message or task from another session to a specialist who is mid-turn waits in its queue and
arrives after the turn; the core does not write to the hub about it (owner decision 2026-10-16: the hub answered "—"
15 times out of 15; from 10-05 to 10-16 the message went straight to the hub for a decision). The hub can act itself:
`hub route <mN|dN> executor|drop|now` — to its executor, drop with a reason, urgently mid-turn. Messages recorded by
the earlier core as "up to the hub" (held) stay until a decision or the end of the turn.
The core does not bring executors up on its own (the earlier auto-start after 90 s — 2026-10-04..05)."""
import tests  # noqa: F401 — do not see the machine's settings
import re
import unittest

from core import executors, forms, reducer
from tests.fakes import lang

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
MAPS = {"channel": "session", "owner": False, "sender": "maps"}
# the earlier Russian heads before a task's words (core/forms.HEADS), as plain text from their fragments in the data
VOICE = lang.legacy("head_voice").replace(r"\s*", " ")                       # "voice: " before a transcription
CONTEXT = re.search("[\u0400-\u04ff]+", lang.legacy("head_context")).group()   # the word "Context"
HUB_WORD = lang.legacy("hub_word").capitalize()
GENERIC = lang.words("words", "topic_generic")   # plain Russian words: "next", "decision", "summary", "next step"…
# the earlier heading of an answer to a question, every optional word of core/forms.HEADS taken: "this is the owner's answer to
# your question"
ANSWER_HEAD = re.sub(r"\(\?:([^()]*)\)\?", r"\1", re.sub(r"\[.(.)\]", r"\1", lang.legacy("head_answer")))
WHAT_TO_DO = "".join(map(chr, (0x0427, 0x0442, 0x043e, 0x20, 0x0434, 0x0435, 0x043b, 0x0430, 0x0442, 0x044c)))   # Russian "what to do"


class Live:
    """The state record by id is always fresh (reduce returns a new state)."""
    def __init__(self, t, table, key):
        self.t, self.table, self.key = t, table, key

    def __getitem__(self, k):
        return self.t.st[self.table][self.key][k]

    def get(self, k, default=None):
        return self.t.st[self.table][self.key].get(k, default)


class BusySpecialist(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-d", role="agent", about="DevOps",
                tools=["k8s"], **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **OWNER)
        for n in ("hub", "devops", "maps"):
            self.ev("session.started", n)
        self.ev("turn.started", "devops", marker=None, prompt_head="long work")        # busy with a turn

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def msg(self, text="is the certificate in prod alive?", to="devops", who=MAPS):
        self.assertIsNone(self.ev("message.received", None, to=to, text=text, **who))
        return Live(self, "messages", f"m{self.seq}")

    def task(self, text="check the cert-manager certificate in prod", who=MAPS):
        self.assertIsNone(self.ev("delegation.created", None, session="devops", context=text,
                                  for_session=who.get("sender"), **who))
        return Live(self, "delegations", f"d{self.seq}")

    def to_hub(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub"]

    def route(self, ref, how, who=HUB, **extra):
        return self.ev("message.routed", ref, route=how, **who, **extra)

    def raise_it(self):
        """Like the daemon: a ready request is a bring-up (executors.events_for)."""
        (name, run), = [(n, r) for n, r in self.st["runs"].items() if r.get("state") == "ready"]
        for type, entity, data in executors.events_for(self.st, name, run, cwd="/k/.claude/worktrees/x", worktree=None):
            self.assertIsNone(self.ev(type, entity, **data), (type, data))
        return name

    def hold(self, m):
        """Like the earlier core (before 2026-10-16): a message "up to the hub" and a hint to the hub."""
        note = reducer.new_message(self.st, f"m{self.seq}h", "hub", f"devops is busy — decide: hub route {m['id']} queue",
                                   1000 + self.seq, origin=forms.ORIGIN_CORE)
        self.st["messages"][m["id"]]["held"] = note["id"]
        return note

    def test_a_sessions_message_to_a_busy_specialist_waits_in_its_queue(self):
        m = self.msg()
        self.assertEqual((m["state"], m.get("held")), ("queued", None))
        self.assertEqual(self.to_hub(), [])
        self.ev("message.received", None, to="devops", text="publish", **OWNER)
        self.ev("turn.stopped", "devops")
        typed = [a for a in self.actions if a.get("do") == "type" and a["session"] == "devops"]
        self.assertEqual(len(typed), 1)

    def test_a_free_specialist_gets_it_directly(self):
        self.ev("turn.stopped", "devops")
        m = self.msg()
        self.assertFalse(m.get("held"))
        self.assertEqual(m["state"], "typing")
        self.assertEqual(self.to_hub(), [])

    def test_what_stays_with_the_specialist(self):
        # owner decision 2026-10-05: the owner's words, messages from the specialist's executors and results of tasks
        # the specialist assigned
        self.msg(text="publish", who=OWNER)
        self.msg(text="a review from the hub", who=HUB)
        self.st["sessions"]["devops-certs"] = {**self.st["sessions"]["maps"], "name": "devops-certs",
                                               "role": "executor", "raised_by": "devops"}
        self.msg(text="done, here is the result", who={**MAPS, "sender": "devops-certs"})
        self.ev("turn.stopped", "devops")
        self.ev("turn.started", "devops", marker=None, prompt_head="x")
        self.ev("delegation.created", None, session="maps", context="draw a layer", for_session="devops",
                channel="session", owner=False, sender="devops")
        d = f"d{self.seq}"
        mid = self.st["delegations"][d]["message"]
        self.ev("turn.started", "maps", marker=mid, prompt_head="x")
        self.ev("turn.result", "maps", text="the layer is ready", marker=mid)                  # task result goes to devops, who is busy
        self.assertEqual(self.st["delegations"][d]["state"], "replied")
        self.assertFalse(any(m.get("held") for m in self.st["messages"].values()))
        self.assertEqual([m for m in self.to_hub() if "busy" in m["text"]], [])

    def test_directions_the_hub_and_sleepers_are_not_held(self):
        self.ev("session.requested", "atlas", cwd="/a", session_id="s-a", **OWNER)   # not a specialist
        self.ev("session.started", "atlas")
        self.ev("turn.started", "atlas", marker=None, prompt_head="x")
        self.ev("turn.started", "hub", marker=None, prompt_head="x")
        self.msg(to="atlas")
        self.msg(to="hub")
        self.assertFalse(any(m.get("held") for m in self.st["messages"].values()))

    def test_queue(self):
        m = self.msg()
        self.assertIn("already in the queue", self.route(m["id"], "queue"))           # already in the queue
        self.hold(m)                                                                   # recorded by the earlier core
        self.assertIsNotNone(self.route(m["id"], "queue", who=MAPS))                  # the hub (or the owner) decides
        self.assertIsNone(self.route(m["id"], "queue"))
        self.assertFalse(m.get("held"))
        self.assertEqual(m["state"], "queued")
        self.ev("turn.stopped", "devops")
        self.assertEqual(m["state"], "typing")
        self.assertIsNotNone(self.route(m["id"], "queue"))                             # no longer up for a decision

    def test_drop_tells_the_sender_why(self):
        m = self.msg()
        self.assertIsNotNone(self.route(m["id"], "drop"))                              # not allowed without a reason
        self.assertIsNone(self.route(m["id"], "drop", why="already done in d7"))
        self.assertEqual(m["state"], "failed")
        told = [x for x in self.st["messages"].values() if x["session"] == "maps"]
        self.assertEqual(len(told), 1)
        self.assertIn("already done in d7", told[0]["text"])
        self.ev("turn.stopped", "devops")
        self.assertFalse([a for a in self.actions if a.get("do") == "type" and a["session"] == "devops"])

    def test_dropping_a_task_closes_it(self):
        d = self.task()
        self.assertIsNone(self.route(d["id"], "drop", why="not for devops, for frontend"))
        self.assertEqual(d["state"], "done")
        self.assertTrue(any("not for devops" in x["text"] for x in self.st["messages"].values() if x["session"] == "maps"))

    def test_a_task_goes_to_an_executor_and_its_result_to_the_sender(self):
        d = self.task()
        m = Live(self, "messages", d["message"])
        self.assertEqual(self.to_hub(), [])
        self.assertIsNone(self.route(d["id"], "executor"))
        name = self.raise_it()
        self.assertTrue(name.startswith("devops-check"))
        x = self.st["sessions"][name]
        self.assertEqual((x["raised_by"], x["tools"], x["profile"]), ("devops", ["k8s"], "s-d"))
        self.assertEqual((m["session"], d["session"], d["for_session"]), (name, name, "maps"))
        self.assertIn("do not raise another one", m["text"])
        self.assertIn("check the cert-manager certificate", m["text"])
        note = [x for x in self.st["messages"].values() if x["session"] == "devops" and m["id"] in x["text"]]
        self.assertEqual(len(note), 1)                                                 # the specialist knows
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=m["id"], prompt_head="x")
        self.ev("turn.result", name, text="the certificate is alive until 12.01", marker=m["id"])
        self.assertTrue(any("alive until 12.01" in x["text"] for x in self.st["messages"].values() if x["session"] == "maps"))

    def test_the_hub_names_the_executor_by_the_subject(self):
        # owner decision 2026-10-05: session names were silly — the name comes from the first words of the task;
        # the hub grasps the point and names it itself: hub route dN executor --name model
        d = self.task(text="by analogy with effort: a model switch for any session")
        self.assertIsNone(self.route(d["id"], "executor", name="Model Switch!"))
        self.assertEqual(list(self.st["runs"]), ["devops-model-switch"])
        d2 = self.task(text="one more about the model")
        self.assertIsNone(self.route(d2["id"], "executor", name="model-switch"))
        self.assertEqual(sorted(self.st["runs"]), ["devops-model-switch", "devops-model-switch-2"])

    def test_a_plain_message_is_not_an_executors_task(self):
        m = self.msg()
        why = self.route(m["id"], "executor")
        self.assertIn("hub task new", why)
        self.assertEqual(m["state"], "queued")

    def test_the_hubs_own_task_to_a_busy_specialist_may_go_to_an_executor(self):
        d = self.task(who=HUB)
        m = Live(self, "messages", d["message"])
        self.assertFalse(m.get("held"))                                                # from the hub — not held
        self.assertIsNone(self.route(d["id"], "executor"))
        name = self.raise_it()
        self.assertEqual(m["session"], name)

    def test_freed_before_the_hub_decided_gets_it_itself(self):
        self.ev("turn.started", "hub", marker=None, prompt_head="x")                  # the hub itself is busy
        m = self.msg()
        note = self.hold(m)                                                            # recorded by the earlier core
        self.ev("turn.stopped", "devops")
        self.assertEqual(m["state"], "typing")
        self.assertFalse(m.get("held"))
        self.assertEqual(self.st["messages"][note["id"]]["state"], "failed")                                      # nothing left for the hub to decide
        self.assertIsNotNone(self.route(m["id"], "queue"))

    def test_a_held_message_is_not_injected(self):
        m = self.msg()
        self.hold(m)
        self.assertIsNotNone(self.ev("message.inject", m["id"], channel="core", owner=False))
        m2 = self.msg(text="and more")                                                   # the new one is queued, as after queue
        self.assertIsNone(self.ev("message.inject", m2["id"], channel="core", owner=False))

    def test_no_worktree_the_task_goes_back_to_the_specialist(self):
        d = self.task()
        self.route(d["id"], "executor")
        name = next(iter(self.st["runs"]))
        self.ev("task.run_failed", name, reason="git worktree add: busy")
        m = Live(self, "messages", d["message"])
        self.assertEqual((m["session"], m.get("handover"), m.get("held")), ("devops", None, None))
        self.assertTrue(any("busy" in x["text"] for x in self.to_hub()))
        self.ev("turn.stopped", "devops")
        self.assertEqual(m["state"], "typing")

    def started_executor(self):
        d = self.task()
        self.route(d["id"], "executor")
        name = self.raise_it()
        self.ev("session.started", name)
        self.ev("turn.started", name, marker=d["message"], prompt_head="x")
        return name, d

    def test_the_owner_or_the_hub_stops_an_executor_at_once(self):
        # 10-04: shutting down all executors quickly — a busy one was not closed ("not free"), and the core brought a
        # killed process up again; hub session stop — end the process at once and park, without handing over work
        name, _ = self.started_executor()
        # someone else's session — no (devops, who brought it up, can: tests/test_command_refusals)
        self.assertIsNotNone(self.ev("session.stop_requested", name, **MAPS))
        self.actions = []
        self.assertIsNone(self.ev("session.stop_requested", name, **HUB))
        x = self.st["sessions"][name]
        self.assertEqual((x["state"], x.get("closing", "none")), ("retired", "none"))
        self.assertEqual(x["state"], "retired")
        self.assertTrue(any(a.get("do") == "release" and a.get("now") and a.get("session") == name for a in self.actions))
        self.ev("session.exited", name, why="released")
        self.assertEqual(self.st["sessions"][name]["state"], "retired")
        self.assertIsNotNone(self.ev("session.stop_requested", "devops", **OWNER))

    def test_supplements_to_the_executors_task_reach_it(self):
        name, d = self.started_executor()
        self.ev("message.received", None, to="devops", about=d["id"], text="and check staging too", **MAPS)
        m = self.st["messages"][f"m{self.seq}"]
        self.assertEqual((m["session"], m.get("held")), (name, None))

    def test_stop_hands_back_the_task_and_the_queue(self):
        name, d = self.started_executor()
        self.ev("message.received", None, to=name, text="one more question on the way", **HUB)
        queued = f"m{self.seq}"
        self.ev("session.stop_requested", name, **HUB)
        self.assertEqual(d["state"], "stopped")
        self.assertEqual(self.st["messages"][queued]["session"], "devops")

    def test_nothing_but_an_explicit_wake_wakes_a_done_executor(self):
        name, _ = self.started_executor()
        self.ev("turn.result", name, text="done")
        self.ev("turn.stopped", name)
        self.ev("session.close_requested", name, retire=True)
        self.ev("session.exited", name, why="sleep")
        self.assertEqual(self.st["sessions"][name]["state"], "retired")
        reducer.new_message(self.st, "mz1", name, "a schedule job", 5000, origin=forms.ORIGIN_SCHEDULE)
        self.assertEqual(self.st["sessions"][name]["state"], "retired")
        self.assertEqual(self.st["messages"]["mz1"]["session"], "devops")

    def test_long_names_fit(self):
        name = executors.auto_name(self.st, "marketing-analytics-team", "Check the certificate in production, urgently")
        self.assertLessEqual(len(name), 40)
        self.assertTrue(reducer.NAME.fullmatch(name))
        self.st["sessions"][name] = {"state": "idle"}
        name2 = executors.auto_name(self.st, "marketing-analytics-team", "Check the certificate in production, urgently")
        self.assertLessEqual(len(name2), 40)
        self.assertNotEqual(name, name2)

    def test_the_name_is_the_task(self):
        # Russian words in Latin letters: the capital, the soft sign and the comma go
        self.assertEqual(executors.auto_name(self.st, "devops", f"{GENERIC[2].capitalize()} {GENERIC[8]}, urgently"),
                         "devops-dalshe-reshenie")
        self.assertEqual(executors.auto_name(self.st, "devops", "check cert-manager now please"),
                         "devops-check-cert-manager")
        self.assertEqual(executors.auto_name(self.st, "devops", f"🎤 {VOICE}…"), "devops-task")
        # 10-04: the name from the task heading — "hub-dev-zadacha-ot"; the gist — after the owner-words / hub / voice markers
        head = f"{lang.legacy('record_task')}{lang.origin('owner')} (#m86181). {lang.legacy('head_words')}:\n> 🎤 {VOICE}"
        gist = f"{lang.word('watch', 'promise_verbs', 7).capitalize()} {GENERIC[5]} as in Claude"
        self.assertEqual(executors.auto_name(self.st, "hub-dev", head + gist), "hub-dev-sdelaju-itog")
        self.assertEqual(executors.auto_name(self.st, "hub-dev", f"{HUB_WORD}, {lang.legacy('from')} agent-dev (a review): "
                                                                 "the core gave a name"), "hub-dev-ot-agent-dev")
        self.assertEqual(executors.auto_name(self.st, "hub-dev", f"{HUB_WORD}: {lang.legacy('who')['owner']} asks to add "
                                                                 "a rule"), "hub-dev-vladelec-asks")
        # 10-05: "hub-dev-otvet-vladelca" — from the heading of an answer to a question
        self.assertEqual(executors.auto_name(self.st, "hub-dev", f"{ANSWER_HEAD} q88647:\n— {WHAT_TO_DO}?: "
                                                                 "Spend and limits"), "hub-dev-chto-delat")
        self.assertEqual(executors.auto_name(self.st, "hub-dev", f"{lang.legacy('record_task')} {lang.legacy('from')} "
                                                                 f"session maps (via the hub).\n{GENERIC[4]}"),
                         "hub-dev-sledujuschij-shag")
        # 10-05: "hub-dev-kontekst-ot" — from the "context from the hub" line at the start of a task
        self.assertEqual(executors.auto_name(self.st, "hub-dev", f"{CONTEXT}{lang.origin('hub')}: "
                                                                 f"{lang.data('words', 'no_data')} of the page «Limits»"),
                         "hub-dev-net-dannyh")


if __name__ == "__main__":
    unittest.main()
