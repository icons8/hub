"""Migration to executors (spec "tasks, specialists, executors", §8): live workers with an open delegation become
executors; with a backlog — executors with a backlog (they don't retire by themselves, they are closed by "finish?");
with neither — a list for the owner "whose / delete / keep", nothing is guessed. Agents stay specialists unchanged. A
repeated run changes nothing."""
import tests  # noqa: F401 — don't see the machine's settings
import unittest

from core import executors, migrate, reducer

OWNER = {"channel": "cli", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}


class Migrate(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-d", role="agent", about="DevOps", **OWNER)
        for n in ("fixer", "tour-epic", "stale"):
            self.ev("session.requested", n, cwd=f"/w/{n}", session_id=f"s-{n}", **OWNER)
            self.ev("session.started", n)
        self.ev("message.received", None, to="hub", text="fix the bug", **OWNER)
        self.ev("delegation.created", None, session="fixer", **{"for": f"m{self.seq}"}, context="", **HUB)

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 100 + self.seq, "type": type,
                                                   "entity": entity, "data": data})
        return why

    def test_the_plan(self):
        plan = migrate.plan(self.st, backlogs={"tour-epic"})
        self.assertEqual(plan["adopt"], [("fixer", None), ("tour-epic", "tour-epic")])
        self.assertEqual(plan["unclear"], ["stale"])
        self.assertNotIn("devops", str(plan))                                # an agent stays a specialist as is

    def test_adopted_sessions_become_executors_and_a_backlog_keeps_one(self):
        for name, backlog in migrate.plan(self.st, backlogs={"tour-epic"})["adopt"]:
            self.assertIsNone(self.ev("task.adopted", name, backlog=backlog, **OWNER))
        f, s = self.st["sessions"]["fixer"], self.st["sessions"]["tour-epic"]
        self.assertEqual((f["role"], f["raised_by"], s["role"], s["backlog"]), ("executor", "hub", "executor", "tour-epic"))
        retiring = dict((n, w) for w, n in executors.retiring(self.st, 10_000))
        self.assertNotIn("tour-epic", retiring)                               # a backlog — leaves only on "finish?"

    def test_a_sleeping_executor_with_open_work_is_not_removed(self):
        # lab run 2026-10-04: the core deleted a migrated sleeping session at once — as if it were a fork that fell asleep
        self.st["sessions"]["fixer"]["state"] = "asleep"
        self.ev("task.adopted", "fixer", **OWNER, ts=500)
        self.assertNotIn("fixer", [n for _, n in executors.retiring(self.st, 10_000)])   # fixer's delegation is in progress
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "fixer"]
        d["state"] = "done"                                                   # the task is delivered, the session sleeps
        self.st["messages"][d["message"]]["state"] = "delivered"
        self.assertNotIn("fixer", [n for _, n in executors.retiring(self.st, 500 + 10)])  # right after the migration — no
        self.assertNotIn(("remove", "fixer"), executors.retiring(self.st, 500 + executors.RETIRE_AFTER + 1))   # sleeps, is not deleted (2026-10-04)

    def test_the_plan_names_those_who_already_delivered(self):
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "fixer"]
        self.ev("turn.started", "fixer", marker=d["message"], prompt_head="x")
        self.ev("turn.result", "fixer", text="done")
        self.assertEqual(migrate.plan(self.st, backlogs={"tour-epic"})["delivered"], ["fixer"])

    def test_a_second_run_changes_nothing(self):
        for name, backlog in migrate.plan(self.st, backlogs={"tour-epic"})["adopt"]:
            self.ev("task.adopted", name, backlog=backlog, **OWNER)
        again = migrate.plan(self.st, backlogs={"tour-epic"})
        self.assertEqual(again["adopt"], [])
        self.assertEqual(again["unclear"], ["stale"])

    def test_an_executor_that_takes_a_backlog_stays_until_it_is_archived(self):
        self.ev("task.adopted", "fixer", **OWNER)
        self.assertIsNone(self.ev("task.backlog", None, slug="bugs", channel="session", owner=False, sender="fixer"))
        self.assertEqual(self.st["sessions"]["fixer"]["backlog"], "bugs")
        self.assertNotIn("fixer", [n for _, n in executors.retiring(self.st, 10_000)])
        self.assertIsNone(self.ev("task.backlog_closed", None, slug="bugs", **OWNER))
        self.assertNotIn("backlog", self.st["sessions"]["fixer"])

    def test_a_direction_the_hub_sets_up_belongs_to_its_session_not_to_the_hub(self):
        # 2026-10-04: the hub ran `hub direction new atlas` itself — the backlog went to the hub, the atlas session stayed
        # a "worker", and the core put it to sleep, closing the task
        hub = {"channel": "session", "owner": False, "sender": "hub"}
        self.ev("session.started", "hub")
        self.assertIsNone(self.ev("task.backlog", None, slug="stale", **hub))      # the stale session already exists
        self.assertEqual(self.st["sessions"]["stale"]["backlog"], "stale")
        self.assertNotIn("backlog", self.st["sessions"]["hub"])
        self.assertIsNone(self.ev("task.backlog", None, slug="atlas", **hub))      # no such session yet — it waits for it
        self.ev("session.requested", "atlas", cwd="/w/atlas", session_id="s-atlas", **OWNER)
        self.assertEqual(self.st["sessions"]["atlas"]["backlog"], "atlas")
        self.ev("session.started", "stale")
        self.ev("delegation.created", None, session="stale", context="the first task of the direction", **hub)
        d = f"d{self.seq}"
        self.ev("turn.started", "stale", marker=self.st["delegations"][d]["message"], prompt_head="x")
        self.ev("turn.result", "stale", text="the plan of the direction is ready")
        self.ev("turn.stopped", "stale")
        self.ev("delegation.done", d, **hub)
        self.assertEqual(self.st["sessions"]["stale"].get("closing", "none"), "none")   # a direction doesn't hand in its work
        self.st["sessions"]["hub"]["backlog"] = "old"                               # the old miss: a backlog on the hub
        self.ev("task.backlog", None, slug="fixer", **hub)
        self.assertNotIn("backlog", self.st["sessions"]["hub"])                    # it is removed

    def test_the_hub_gives_a_direction_to_a_session_with_another_name(self):
        # 2026-10-08: the owner asked to "make a direction" out of executor fixer's work; the hub ran `hub direction new
        # payments` — the backlog waited for a "payments" session that doesn't exist and never will, and nothing appeared in "Directions"
        self.assertIsNone(self.ev("task.backlog", None, slug="payments", **HUB))         # the old miss: it waits for the name
        self.assertIsNone(self.ev("task.backlog", None, slug="payments", session="fixer", **HUB))
        self.assertEqual(self.st["sessions"]["fixer"]["backlog"], "payments")
        self.assertNotIn("payments", self.st.get("pending_backlogs") or {})              # nothing left to wait for
        self.assertIsNone(self.ev("task.backlog", None, slug="atlas", session="newbie", **HUB))   # it doesn't exist yet
        self.ev("session.requested", "newbie", cwd="/w/n", session_id="s-n", **OWNER)
        self.assertEqual(self.st["sessions"]["newbie"]["backlog"], "atlas")
        mine = {"channel": "session", "owner": False, "sender": "stale"}
        self.assertIsNone(self.ev("task.backlog", None, slug="bugs", session="stale", **mine))   # for itself — allowed
        self.assertEqual(self.st["sessions"]["stale"]["backlog"], "bugs")
        self.assertIsNotNone(self.ev("task.backlog", None, slug="bugs", session="tour-epic", **mine))  # someone else's — no
        self.assertNotIn("backlog", self.st["sessions"]["tour-epic"])
        self.assertIsNone(self.ev("task.backlog", None, slug="ops", session="tour-epic", **OWNER))   # the owner — yes
        self.assertEqual(self.st["sessions"]["tour-epic"]["backlog"], "ops")

    def test_a_session_that_leads_a_direction_does_not_take_a_second_one(self):
        # a session that already leads `maps` ran `hub direction new tiles`: the backlog moved to tiles and `maps` lost its
        # lead without a word, while tiles had no session of its own
        mine = {"channel": "session", "owner": False, "sender": "stale"}
        self.assertIsNone(self.ev("task.backlog", None, slug="maps", **mine))           # no backlog yet: it leads
        self.assertEqual(self.st["sessions"]["stale"]["backlog"], "maps")
        why = self.ev("task.backlog", None, slug="tiles", **mine)                         # a second one: refused
        self.assertIn("hub direction new tiles --session", why)
        self.assertEqual(self.st["sessions"]["stale"]["backlog"], "maps")
        self.assertNotIn("tiles", self.st.get("pending_backlogs") or {})
        self.assertIsNone(self.ev("task.backlog", None, slug="maps", **mine))           # the same one again: not a refusal
        self.assertEqual(self.st["sessions"]["stale"]["backlog"], "maps")
        # the hub starts it with its own lead; a session leading one is not hurt by the hub's other directions
        self.assertIsNone(self.ev("task.backlog", None, slug="tiles", **HUB))
        self.assertEqual(self.st["pending_backlogs"]["tiles"]["slug"], "tiles")
        self.assertIsNone(self.ev("task.backlog", None, slug="tiles", session="fixer", **HUB))
        self.assertEqual(self.st["sessions"]["fixer"]["backlog"], "tiles")
        self.assertEqual(self.st["sessions"]["stale"]["backlog"], "maps")

    def test_archiving_a_renamed_direction_frees_the_session_that_took_it_under_the_old_name(self):
        mine = {"channel": "session", "owner": False, "sender": "stale"}
        self.ev("task.backlog", None, slug="maps", **mine)
        self.assertIsNone(self.ev("task.backlog_closed", None, slug="atlas", slugs=["atlas", "maps"], **OWNER))
        self.assertNotIn("backlog", self.st["sessions"]["stale"])
        self.assertIsNone(self.ev("task.backlog", None, slug="tiles", **mine))   # it may lead a new one now
        self.assertEqual(self.st["sessions"]["stale"]["backlog"], "tiles")
        self.assertIsNone(self.ev("task.backlog_closed", None, slug="tiles", **OWNER))   # an event with no names, as before
        self.assertIsNone(self.ev("task.backlog_closed", None, **OWNER))   # no name at all: no session is touched, no crash
        self.assertNotIn("backlog", self.st["sessions"]["stale"])

    def test_the_hub_takes_a_lead_off_a_direction(self):
        # 2026-11-06: an agent ran `hub direction lead orbit` without a name — it became the lead itself; then the hub
        # gave the direction to the session "orbit", and the backlog stayed on the agent: two leads, and the agent got
        # the direction's summary after every compaction
        self.ev("session.started", "hub")
        agent = {"channel": "session", "owner": False, "sender": "devops"}
        self.assertIsNone(self.ev("task.backlog", None, slug="orbit", **agent))
        self.assertIsNone(self.ev("task.backlog", None, slug="orbit", session="fixer", **HUB))
        self.assertEqual({n for n, s in self.st["sessions"].items() if s.get("backlog") == "orbit"}, {"devops", "fixer"})
        self.assertIsNone(self.ev("task.backlog_dropped", None, slug="orbit", session="devops", **HUB))
        self.assertNotIn("backlog", self.st["sessions"]["devops"])
        self.assertEqual(self.st["sessions"]["fixer"]["backlog"], "orbit")
        # the last lead may go too: the direction is then like one nobody has taken yet
        self.assertIsNone(self.ev("task.backlog_dropped", None, slug="orbit", session="fixer", **OWNER))
        self.assertNotIn("backlog", self.st["sessions"]["fixer"])

    def test_a_lead_is_found_by_the_former_name_of_a_renamed_direction(self):
        # the session keeps the name under which it took the direction; the CLI sends the folder's name and the old ones too
        self.ev("session.started", "hub")
        self.ev("task.backlog", None, slug="payments", session="fixer", **HUB)
        self.assertIsNotNone(self.ev("task.backlog_dropped", None, slug="billing", session="fixer", **HUB))
        self.assertIsNone(self.ev("task.backlog_dropped", None, slug="billing", slugs=["billing", "payments"],
                                  session="fixer", **HUB))
        self.assertNotIn("backlog", self.st["sessions"]["fixer"])

    def test_only_the_hub_or_the_owner_drops_a_lead_and_only_of_its_own_direction(self):
        self.ev("session.started", "hub")
        self.ev("task.backlog", None, slug="orbit", session="fixer", **HUB)
        self.ev("task.backlog", None, slug="ops", session="stale", **HUB)
        mine = {"channel": "session", "owner": False, "sender": "fixer"}
        self.assertIsNotNone(self.ev("task.backlog_dropped", None, slug="orbit", session="fixer", **mine))   # not for itself
        self.assertIsNotNone(self.ev("task.backlog_dropped", None, slug="orbit", session="fixer",
                                     channel="session", owner=False, sender="stale"))
        self.assertIsNotNone(self.ev("task.backlog_dropped", None, slug="orbit", session="stale", **HUB))   # leads another
        self.assertIsNotNone(self.ev("task.backlog_dropped", None, slug="orbit", session="nobody", **HUB))
        self.assertIsNotNone(self.ev("task.backlog_dropped", None, slug="orbit", **HUB))                    # no session named
        self.assertEqual((self.st["sessions"]["fixer"]["backlog"], self.st["sessions"]["stale"]["backlog"]),
                         ("orbit", "ops"))
        self.assertIsNone(self.ev("task.backlog_dropped", None, slug="ops", session="stale", **HUB))   # the allowed one works
        self.assertNotIn("backlog", self.st["sessions"]["stale"])

    def test_a_direction_that_still_waits_for_its_session_can_be_dropped(self):
        self.ev("session.started", "hub")
        self.ev("task.backlog", None, slug="atlas", session="newbie", **HUB)
        self.assertIsNone(self.ev("task.backlog_dropped", None, slug="atlas", session="newbie", **HUB))
        self.assertNotIn("newbie", self.st.get("pending_backlogs") or {})
        self.ev("session.requested", "newbie", cwd="/w/n", session_id="s-n", **OWNER)
        self.assertNotIn("backlog", self.st["sessions"]["newbie"])

    def test_whoever_asked_for_the_task_is_the_raiser(self):
        self.ev("session.requested", "frontend", cwd="/fe", session_id="s-fe", role="agent", about="FE", **OWNER)
        self.ev("delegation.created", None, session="stale", for_session="frontend", context="TASK-10", **HUB)
        self.assertEqual(migrate.raiser_of(self.st, "stale"), "frontend")
        self.assertIsNone(migrate.raiser_of(self.st, "fixer"))                 # from the owner via the hub — the hub raised it

    def test_a_direction_belongs_to_the_hub_whoever_asked_it_last(self):
        # live journal 2026-10-04: direction tour-epic was "raised" by billing — which merely asked it last
        self.ev("delegation.created", None, session="tour-epic", for_session="fixer", context="a question", **HUB)
        self.assertEqual(migrate.raiser_of(self.st, "tour-epic"), "fixer")
        self.assertIsNone(migrate.raiser_of(self.st, "tour-epic", backlog="tour-epic"))

    def test_only_the_owner_adopts(self):
        self.assertIsNotNone(self.ev("task.adopted", "fixer", **HUB))


class Cli(unittest.TestCase):
    def test_hub_migrate_backs_up_adopts_and_asks_about_the_rest(self):
        import os
        import subprocess
        import sys
        import tempfile
        from core import journal, spool
        state = tempfile.mkdtemp(prefix="mig-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER})
        for n in ("fixer", "tour-epic", "stale"):
            j.append("session.requested", n, {"cwd": f"/w/{n}", "session_id": f"s-{n}", **OWNER})
        j.append("message.received", None, {"to": "hub", "text": "fix", **OWNER})
        j.append("delegation.created", None, {"session": "fixer", "for": "m5", "context": "", **HUB})
        os.makedirs(os.path.join(state, "directions", "tour-epic"))
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, hub, "migrate"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        evs = [e for _, e in spool.take(os.path.join(state, "spool"))]
        adopted = {(e["entity"], e["data"].get("backlog")) for e in evs if e["type"] == "task.adopted"}
        self.assertEqual(adopted, {("fixer", None), ("tour-epic", "tour-epic")})
        (ask,) = [e for e in evs if e["type"] == "message.received"]
        self.assertIn("stale", ask["data"]["text"])
        backups = os.listdir(os.path.join(state, "backup"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(os.path.exists(os.path.join(state, "backup", backups[0], "journal.jsonl")))
        self.assertIn("stale", r.stdout)

    def test_open_tasks_get_their_records_even_when_nobody_is_adopted(self):
        # after the live hub was migrated (2026-10-04) the owner asked whether task files had been built for the old ones — they had not
        import os
        import subprocess
        import sys
        import tempfile
        from core import journal
        state = tempfile.mkdtemp(prefix="mig-rec-")
        j = journal.Journal(os.path.join(state, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": "/h", "session_id": "s-hub", "kind": "hub", **OWNER})
        j.append("session.requested", "maps", {"cwd": "/w", "session_id": "s-maps", "role": "agent", **OWNER})
        open_ = j.append("delegation.created", None, {"session": "maps", "context": "map of offices\nwhen ready: PR", **HUB})
        shut = j.append("delegation.created", None, {"session": "maps", "context": "old", **HUB})
        j.append("delegation.done", f"d{shut['seq']}", {**OWNER})
        hub = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
        env = {**os.environ, "HUB_STATE_DIR": state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        r = subprocess.run([sys.executable, hub, "migrate"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        self.assertEqual([f for f in os.listdir(os.path.join(state, "tasks")) if f.endswith(".md")], [f"d{open_['seq']}.md"])
        with open(os.path.join(state, "tasks", f"d{open_['seq']}.md"), encoding="utf-8") as f:
            self.assertIn("map of offices", f.read())
        self.assertIn("task records: 1", r.stdout)


if __name__ == "__main__":
    unittest.main()
