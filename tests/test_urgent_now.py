"""Urgent means at once, without the queue and the watchdog (owner decision 2026-10-09: if the hub knows a task is
urgent, it sends it urgently right away instead of just queueing it). The hub (or the owner) judges urgency itself:
`hub send <s> --now`, `hub task new <s> --now`, `hub route <mN|dN> now`. For a busy session the core types it mid-turn
at once, without waiting for the watchdog's estimate (Haiku) or the end of the turn. A task inserted mid-turn gets the
result of that turn, like the task the turn began with. Other sessions do not get the flag: the hub decides urgency."""
import tests  # noqa: F401 — hide the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import journal, reducer, spool

OWNER = {"channel": "web", "owner": True}
HUB = {"channel": "session", "owner": False, "sender": "hub"}
MAPS = {"channel": "session", "owner": False, "sender": "maps"}
BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class Now(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.requested", "devops", cwd="/k", session_id="s-d", role="agent", about="DevOps", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", about="Maps", **OWNER)
        for n in ("hub", "devops", "maps"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def typed(self, mid):
        return [a for a in self.acts if a["do"] == "type" and a["message"] == mid]

    def busy(self, marker=None):
        self.ev("turn.started", "devops", marker=marker, prompt_head="long work")

    def send(self, text, who=HUB, **extra):
        why = self.ev("message.received", None, to="devops", text=text, **who, **extra)
        return why, self.st["messages"].get(f"m{self.seq}")

    def task(self, text, who=HUB, **extra):
        why = self.ev("delegation.created", None, session="devops", context=text, for_session=who.get("sender"),
                      **who, **extra)
        d = self.st["delegations"].get(f"d{self.seq}")
        return why, d, d and self.st["messages"][d["message"]]

    def test_the_hubs_urgent_message_is_typed_mid_turn_at_once(self):
        self.busy()
        _, plain = self.send("look at the README later")
        self.assertEqual(plain["state"], "queued")                        # no flag, as before: queue and watchdog
        why, m = self.send("stop: prod is down, roll back the last release", now=True)
        self.assertIsNone(why)
        self.assertEqual((m["state"], m.get("injected"), m.get("urgent")), ("delivered", True, True))
        (typed,) = self.typed(m["id"])
        self.assertIn("(urgent — came in the middle of your turn", typed["text"])
        self.assertIn("roll back the last release", typed["text"])

    def test_urgent_words_of_the_owner_and_a_task_addition_go_in_mid_turn(self):
        """`hub send --now --for mN` and `--now --about dN` are the same urgent ones, not the queue (the flag used to
        get lost in the CLI)."""
        self.busy()
        self.ev("message.received", None, to="hub", text="reply", **OWNER)
        word = f"m{self.seq}"
        for extra in ({"for": word}, {"about": "d1"}):
            why, m = self.send("roll back the release", now=True, **extra)
            self.assertIsNone(why, extra)
            self.assertEqual((m["state"], m.get("injected"), m.get("urgent")), ("delivered", True, True), extra)

    def test_only_the_hub_or_the_owner_decides_it_is_urgent(self):
        self.busy()
        why, _ = self.send("urgent, take a look", who=MAPS, now=True)
        self.assertIn("is decided by the hub", why or "")
        why, _, _ = self.task("urgent, take a look", who=MAPS, now=True)
        self.assertIn("is decided by the hub", why or "")
        why, m = self.send("stop", who=OWNER, now=True)                  # the owner too
        self.assertIsNone(why)
        self.assertTrue(m.get("injected"))

    def test_waiting_for_a_yes_it_goes_in_as_soon_as_the_turn_goes_on(self):
        self.ev("permission.requested", "devops", request_id="r1", tool="Bash", input={"command": "x"})
        _, m = self.send("urgent: roll back the release", now=True)
        self.assertEqual(m["state"], "queued")                            # waits for a "yes": nowhere to insert
        self.ev("permission.answered", "devops", request_id="r1", behavior="allow", **OWNER)
        self.assertTrue(self.st["messages"][m["id"]].get("injected"))     # the turn moved on: at once, without the watchdog

    def test_a_free_session_gets_it_first(self):
        self.busy()
        _, old = self.send("look at the README later")
        _, m = self.send("/compact", now=True)                            # a command goes only with a new turn
        self.assertEqual(m["state"], "queued")
        self.ev("turn.result", "devops", text="ok")
        self.assertEqual(self.st["messages"][m["id"]]["state"], "typing")   # first, though it arrived later
        self.assertEqual(self.st["messages"][old["id"]]["state"], "queued")

    def test_an_urgent_task_mid_turn_is_answered_by_that_turn_with_the_first_one(self):
        self.urgent_mid_turn(reported=True)

    def test_even_when_claude_code_does_not_report_the_mid_turn_input(self):
        self.urgent_mid_turn(reported=False)   # journal of 09.10: some insertions come without a marked turn

    def test_an_urgent_task_in_a_turn_without_a_task_gets_its_result(self):
        _, m0 = self.send("check the logs", who=OWNER)
        self.busy(marker=m0["id"])
        _, d, m = self.task("urgent: renew the certificate", now=True)
        self.ev("turn.result", "devops", text="renewed")
        self.assertEqual(self.st["delegations"][d["id"]]["reply"], "renewed")

    def urgent_mid_turn(self, reported):
        _, first, fm = self.task("build the alerts report")
        self.busy(marker=fm["id"])
        why, d, m = self.task("urgent: the certificate in prod has expired, renew it", now=True)
        self.assertIsNone(why)
        self.assertEqual((m["state"], m.get("injected")), ("delivered", True))
        (typed,) = self.typed(m["id"])
        self.assertIn("(urgent task", typed["text"])
        self.assertIn(d["id"], typed["text"])
        if reported:
            self.ev("turn.started", "devops", marker=m["id"], prompt_head="x")   # Claude Code took the insertion mid-turn
        self.ev("turn.result", "devops", text="certificate renewed; alerts report is in the README")
        for x in (first, d):
            got = self.st["delegations"][x["id"]]
            self.assertEqual((got["state"], got["reply"]), ("replied", "certificate renewed; alerts report is in the README"))
        # both tasks go to the hub (it is their recipient, for_session=hub): as a full result "reply to task dN", not a
        # separate REPLIED about the same thing (14.10: the hub's task used to produce two messages)
        got = [x for x in self.st["messages"].values() if x["session"] == "hub" and "reply to task" in x["origin"]]
        self.assertEqual(sorted(x["task"] for x in got), sorted(x["id"] for x in (first, d)))
        self.assertFalse([x for x in self.st["messages"].values()
                          if x["session"] == "hub" and "REPLIED" in (x["text"] or "")])

    def test_an_urgent_task_to_a_free_session_is_just_a_task(self):
        why, d, m = self.task("urgent: renew the certificate", now=True)
        self.assertIsNone(why)
        self.assertEqual(m["state"], "typing")
        self.ev("turn.started", "devops", marker=m["id"], prompt_head="x")
        self.ev("turn.result", "devops", text="renewed")
        self.assertEqual(self.st["delegations"][d["id"]]["reply"], "renewed")

    def test_route_now_a_queued_message_or_task(self):
        self.busy()
        _, m = self.send("prod is down at maps — check the ingress", who=MAPS)
        self.assertEqual((m["state"], m.get("held")), ("queued", None))                 # queued, the hub is not written to
        self.assertIsNotNone(self.ev("message.routed", m["id"], route="now", **MAPS))   # the hub decides
        self.assertIsNone(self.ev("message.routed", m["id"], route="now", **HUB))
        m = self.st["messages"][m["id"]]
        self.assertEqual((m["state"], m.get("injected"), m.get("held")), ("delivered", True, None))
        _, d, dm = self.task("renew the certificate", who=MAPS)
        self.assertIsNone(self.ev("message.routed", d["id"], route="now", **HUB))
        self.assertTrue(self.st["messages"][dm["id"]].get("injected"))

    def test_route_now_the_hubs_own_queued_message(self):
        # the hub already sent without the flag, the watchdog rated it low, and then the hub changed its mind: now
        self.busy()
        _, m = self.send("roll back the release")
        self.ev("message.weighed", m["id"], p=0.2, channel="cli", owner=True)
        self.assertIsNone(self.ev("message.routed", m["id"], route="now", **HUB))
        self.assertTrue(self.st["messages"][m["id"]].get("injected"))
        self.assertIsNotNone(self.ev("message.routed", m["id"], route="now", **HUB))    # already delivered


class Cli(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="now-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for n in ("maps", "devops"):
            j.append("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", "channel": "cli", "owner": True})

    def hub(self, *args):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "hub"}
        r = subprocess.run([sys.executable, BIN, *args], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]

    def test_flags(self):
        (ev,) = self.hub("send", "devops", "--now", "roll back", "release")
        self.assertEqual((ev["data"]["to"], ev["data"]["text"], ev["data"].get("now")), ("devops", "roll back release", True))
        ev = self.hub("task", "new", "devops", "--now", "renew the certificate")[-1]
        self.assertEqual((ev["type"], ev["data"]["context"], ev["data"].get("now")),
                         ("delegation.created", "renew the certificate", True))
        ev = self.hub("route", "m7", "now")[-1]
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["route"]), ("message.routed", "m7", "now"))


if __name__ == "__main__":
    unittest.main()
