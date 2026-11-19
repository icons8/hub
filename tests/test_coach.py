"""coach — a watcher of the fleet (owner request 2026-10-03: track when an executor has outgrown its role and should
become a direction, or an executor an agent; watch the agents and suggest retraining them; 2026-10-05 — following the
model of specialists, executors and directions). Counters over the journal, no model."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import tempfile
import unittest

from modules.coach import coach

H = 3600
NOW = 1_000_000.0


class Coach(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="coach-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.lines, self.seq = [], 0

    def ev(self, ts, type, entity=None, **data):
        self.seq += 1
        self.lines.append({"seq": self.seq, "ts": NOW + ts, "type": type, "entity": entity, "data": data})

    def session(self, name, cwd, ts=-100 * H, **data):
        self.ev(ts, "session.requested", name, cwd=cwd, session_id=f"s-{name}", **data)

    def turns(self, name, start, hours, n, tools=10, cost=0.5):
        for i in range(n):  # cost_usd in a turn result is the sum over the process lifetime, as in claude
            t = start + hours * H * i / max(1, n - 1)
            self.ev(t, "turn.started", name, marker=None)
            for _ in range(tools):
                self.ev(t, "turn.tool", name, name="Bash")
            self.ev(t + 1, "turn.result", name, text="ok", cost_usd=cost * (i + 1))

    def report(self, directions=()):
        with open(os.path.join(self.dir, "journal.jsonl"), "w") as f:
            f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self.lines))
        return coach.analyze(self.dir, now=NOW, directions=set(directions))

    def kinds(self, rep):
        return {(s["kind"], s["subject"]) for s in rep["suggestions"]}

    def executor(self, name, cwd, by="hub", ts=-100 * H):
        self.session(name, cwd, ts=ts, executor=True, raised_by=by)

    def test_executor_that_grew_is_offered_as_a_direction(self):
        self.session("hub", "/hub", kind="hub")
        self.executor("billing-be", "/code/backend")
        self.turns("billing-be", -40 * H, 30, 40, tools=30)
        self.ev(-10 * H, "session.compacted", "billing-be")
        self.executor("quick", "/code/web")
        self.turns("quick", -5 * H, 1, 5)
        self.executor("newsletter", "/code/backend")                     # already a direction — do not suggest
        self.turns("newsletter", -40 * H, 30, 40, tools=30)
        self.executor("reports", "/code/reports")                            # backlog — a direction
        self.turns("reports", -40 * H, 30, 40, tools=30)
        self.ev(-30 * H, "task.backlog", None, sender="reports", slug="reports")
        rep = self.report(directions={"newsletter"})
        self.assertEqual(self.kinds(rep), {("direction", "billing-be")})
        (s,) = rep["suggestions"]
        self.assertTrue(s["why"].startswith("outgrown its role: "), s["why"])

    def test_a_lead_taken_off_by_the_hub_is_an_executor_again_for_the_coach(self):
        self.session("hub", "/hub", kind="hub")
        self.executor("reports", "/code/reports")
        self.turns("reports", -40 * H, 30, 40, tools=30)
        self.ev(-30 * H, "task.backlog", None, sender="reports", slug="reports")
        self.ev(-3 * H, "task.backlog_dropped", None, sender="reports", slug="reports", session="reports",
                channel="session")   # not by the hub: the core refused it
        self.assertEqual(self.kinds(self.report()), set())
        self.ev(-2 * H, "task.backlog_dropped", None, sender="hub", slug="reports", session="reports", channel="session")
        self.assertEqual(self.kinds(self.report()), {("direction", "reports")})

    def test_a_lead_taken_off_by_the_owner_is_an_executor_again_for_the_coach(self):
        self.session("hub", "/hub", kind="hub")
        self.executor("reports", "/code/reports")
        self.turns("reports", -40 * H, 30, 40, tools=30)
        self.ev(-30 * H, "task.backlog", None, sender="reports", slug="reports")
        self.ev(-2 * H, "task.backlog_dropped", None, slug="reports", session="reports", channel="cli", owner=True)
        self.assertEqual(self.kinds(self.report()), {("direction", "reports")})

    def test_a_foreign_file_among_archives_is_not_read(self):
        # a copy of an archive under another name is not an archive: the watcher crashed on it (number from the name) and would count it twice
        self.session("hub", "/hub", kind="hub")
        self.executor("billing-be", "/code/backend")
        self.turns("billing-be", -40 * H, 30, 40, tools=30)
        os.makedirs(os.path.join(self.dir, "journal-archive"))
        with open(os.path.join(self.dir, "journal-archive", "journal-old-copy.jsonl"), "w") as f:
            f.write("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in self.lines))
        self.lines = self.lines[:1]
        self.assertEqual(self.kinds(self.report()), set())

    def test_executor_people_return_to_is_offered_as_a_direction(self):
        self.session("hub", "/hub", kind="hub")
        self.executor("reports", "/code/bi", by="hub")
        self.turns("reports", -20 * H, 10, 4)
        for t in (-20, -12):
            self.ev(t * H, "delegation.created", None, session="reports", for_session="hub", context="x")
        self.ev(-8 * H, "session.wake_requested", "reports", sender="ops")      # rejected by the core — it never happened
        self.ev(-8 * H, "core.rejected", "reports", of=self.seq)
        rep = self.report()
        self.assertEqual(self.kinds(rep), set())                                 # two tasks — not yet
        self.ev(-4 * H, "delegation.created", None, session="reports", for_session="hub", context="y")
        self.ev(-3 * H, "session.wake_requested", "reports", sender="hub")
        (s,) = self.report()["suggestions"]
        self.assertEqual((s["kind"], s["subject"]), ("direction", "reports"))
        self.assertEqual(s["why"], "people keep coming back to it: tasks this week 3")

    def test_repeated_work_without_a_specialist_and_past_a_specialist(self):
        self.session("devops", "/code/k8s", role="agent", about="DevOps")
        for n in ("k8s-a", "k8s-b", "k8s-c"):                        # the hub started them bypassing its repo's specialist
            self.executor(n, "/code/k8s/.claude/worktrees/x" if n == "k8s-c" else "/code/k8s", ts=-48 * H)
            self.turns(n, -48 * H, 1, 3)
        for n in ("pay-1", "pay-2", "pay-3"):                           # no specialist
            self.executor(n, "/code/mono", ts=-24 * H)
            self.turns(n, -24 * H, 1, 3)
        self.session("devops-2", "/code/k8s", ts=-24 * H, fork_of="devops")   # a fork from an old journal — not counted
        self.turns("devops-2", -24 * H, 1, 3)
        self.session("old", "/code/mono", ts=-30 * 24 * H)                    # older than a week — not counted
        rep = self.report()
        self.assertEqual(self.kinds(rep), {("bypass", "devops"), ("agent", "/code/mono")})
        (s,) = [x for x in rep["suggestions"] if x["kind"] == "bypass"]
        self.assertEqual(s["sessions"], ["k8s-a", "k8s-b", "k8s-c"])

    def test_repos_inside_a_specialists_folder_are_his_zone(self):
        # 2026-10-07 (owner decision): a frontend specialist's repos live inside its folder (<folder>/repos/*); within
        # a week other sessions started three executors there bypassing it — the watcher advised "create a specialist"
        self.session("web", "/code/web-hub", role="agent", about="frontend")
        for n, r in (("icon-fix", "icons"), ("land-fix", "landing"), ("icon-2", "icons/.claude/worktrees/icon-2")):
            self.executor(n, f"/code/web-hub/repos/{r}", by="maps", ts=-24 * H)
            self.turns(n, -24 * H, 1, 3)
        self.assertEqual(self.kinds(self.report()), {("bypass", "web")})

    def test_specialists_own_executors_and_the_hub_sandbox_are_not_past_him(self):
        # 2026-10-05: hub-dev executors in its repo counted as "work bypassing the agent"
        self.session("web", "/code/web", role="agent", about="frontend")
        for n in ("web-a", "web-b", "web-c"):
            self.executor(n, f"/code/web/.claude/worktrees/{n}", by="web", ts=-24 * H)
            self.turns(n, -24 * H, 1, 3)
        sandbox = os.path.join(self.dir, "sandbox")
        self.session("notes", sandbox, role="agent", about="notes")
        for n in ("memo-1", "memo-2", "memo-3"):
            self.executor(n, sandbox, ts=-24 * H)
            self.turns(n, -24 * H, 1, 3)
        self.assertEqual(self.kinds(self.report()), set())

    def test_specialist_health_counts_his_executors(self):
        self.session("search", "/code/search", role="agent", about="search")
        self.turns("search", -50 * H, 40, 10, cost=2.0)
        for t in (-30, -20):
            self.ev(t * H, "watch.alert", "search", kind="hung", reason="silent")
        self.ev(-6 * H, "turn.result", "search", text="", is_error=True, cost_usd=1.0)   # a stop is not an error; a new process
        rep = self.report()
        self.assertNotIn(("retrain", "search"), self.kinds(rep))                         # 2 alarms and a stop — too few
        self.executor("search-fix", "/code/search", by="search", ts=-6 * H)
        self.ev(-5 * H, "turn.result", "search-fix", text="API Error: 500", is_error=True, cost_usd=1.5)
        (s,) = [x for x in self.report()["suggestions"] if x["kind"] == "retrain"]
        self.assertEqual((s["subject"], s["sessions"]), ("search", ["search", "search-fix"]))
        self.assertIn("its executors 1 more", s["why"])

    def test_idle_specialist_is_offered_to_let_go_unless_his_executors_work(self):
        self.session("one-off", "/code/x", ts=-20 * 24 * H, role="agent", about="task")
        self.turns("one-off", -15 * 24 * H, 1, 3)
        self.assertIn(("idle_agent", "one-off"), self.kinds(self.report()))
        self.executor("one-off-fix", "/code/x", by="one-off", ts=-2 * 24 * H)
        self.turns("one-off-fix", -2 * 24 * H, 1, 3)
        self.assertNotIn(("idle_agent", "one-off"), self.kinds(self.report()))

    def test_a_session_asked_by_a_working_session_is_not_created(self):
        # 2026-10-29: the watcher offered to let go of the specialist bug-reporter "without work 9 days", but there was
        # never such a session: a working session asked for it, and the core only passes that request to the hub
        # (core/machines.py, from_session -> ask_hub); the hub created the specialist under another name
        self.session("hub", "/hub", ts=-30 * 24 * H, kind="hub")
        self.session("maps", "/code/maps", ts=-20 * 24 * H, role="agent", about="maps", channel="web", owner=True)
        self.turns("maps", -15 * 24 * H, 1, 3)
        self.session("bug-reporter", "/code/bugs", ts=-9 * 24 * H, role="agent", channel="session", sender="maps")
        self.session("report-bug", "/code/bugs", ts=-9 * 24 * H, role="agent", channel="session", sender="hub")
        kinds = self.kinds(self.report())
        self.assertNotIn("bug-reporter", coach._sessions(self.dir, NOW))
        self.assertIn(("idle_agent", "report-bug"), kinds)                      # the hub's request creates it
        self.assertIn(("idle_agent", "maps"), kinds)
        # a mini-hub's executor (hub task run) is written by the core, without a channel: it counts as before
        self.executor("maps-fix", "/code/maps", by="maps", ts=-2 * 24 * H)
        self.turns("maps-fix", -2 * 24 * H, 1, 3)
        self.assertNotIn(("idle_agent", "maps"), self.kinds(self.report()))
        self.assertEqual(coach._sessions(self.dir, NOW)["maps-fix"]["raised_by"], "maps")


if __name__ == "__main__":
    unittest.main()
