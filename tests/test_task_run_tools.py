"""`hub task run … --tools a,b`: the executor starts with the tools already. Without this the hub granted them seconds
after the start, the executor was already in a turn and did the task without them, and the core restarted it with them
only after the turn — the second turn redid the work (twice in a day). Granting before
the start is impossible: the session does not exist yet."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import i18n, tools
from core import executors, reducer, views
from tests.test_executors import OWNER, by

CATALOG = {"research@kit": {"kind": "plugin", "risk": []}, "mail": {"kind": "mcp", "risk": ["owner-data"]},
           "browser:mac": {"kind": "browser", "risk": [], "exclusive": True},
           "jira": {"kind": "mcp", "risk": ["external", "owner-data"], "base": True},
           "conduit": {"kind": "mcp", "risk": ["owner-data", "prod"]}}


class TaskRunTools(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "frontend", cwd="/fe", session_id="s-fe", role="agent", tools=["design@kit"],
                **OWNER)
        self.ev("session.started", "frontend")
        self.ev("catalog.loaded", tools=CATALOG)
        self.ev("session.started", "hub")

    def ev(self, type, entity=None, src=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data, **({"src": src} if src else {})})
        self.actions += acts
        return why

    def run_task(self, name, **extra):
        return self.ev("task.run_requested", name, task="fix the CI secrets", **by("frontend"), **extra)

    def start(self, name, raiser="frontend", cwd="/fe/.claude/worktrees/x", repo_tools=(), src=None):
        """Like the daemon: the executor is started and has received the task (core/executors.events_for). repo_tools
        — what the daemon found in the executor's folder (catalog.repo_tools); src — whose event it is."""
        for type, entity, data in executors.events_for(self.st, name, self.st["runs"][name], cwd=cwd, worktree=None,
                                                       repo_tools=repo_tools):
            self.assertIsNone(self.ev(type, entity, src=src, **data), (type, data))

    def spawn_of(self, name):
        return [a for a in self.actions if a["do"] == "spawn" and a.get("session") == name]

    def test_the_executor_starts_with_the_tools_and_is_not_raised_again(self):
        self.assertIsNone(self.ev("task.run_requested", "seeds", task="collect the shops", tools=["research@kit"],
                                  **by("hub")))
        self.start("seeds", raiser="hub", cwd="/sb")
        x = self.st["sessions"]["seeds"]
        self.assertEqual((x["tools"], x["dirty"]), (["research@kit"], False))
        (spawn,) = self.spawn_of("seeds")
        self.assertEqual(spawn["tools"], ["research@kit"])
        self.ev("session.started", "seeds")
        self.ev("turn.started", "seeds", marker=None)
        self.ev("turn.result", "seeds", text="done")
        self.ev("turn.stopped", "seeds")
        self.assertEqual(len(self.spawn_of("seeds")), 1)                     # not restarted after the turn
        self.assertFalse([m for m in self.st["messages"].values()
                          if m["session"] == "seeds" and m["text"].startswith("Connected")])

    def test_the_raisers_own_tools_stay(self):
        self.run_task("frontend-ci", tools=["research@kit"])
        self.start("frontend-ci")
        self.assertEqual(self.st["sessions"]["frontend-ci"]["tools"], ["design@kit", "research@kit"])

    def test_an_unknown_tool_rejects_the_run(self):
        why = self.ev("task.run_requested", "seeds", task="collect", tools=["nope"], **by("hub"))
        self.assertIn("nope", why)
        self.assertNotIn("seeds", self.st.get("runs") or {})

    def test_a_risky_tool_without_the_owners_word_waits_for_him_and_the_executor_starts_anyway(self):
        self.ev("task.run_requested", "teams-ask", task="ask", tools=["mail", "research@kit"], **by("hub"))
        self.start("teams-ask", raiser="hub", cwd="/sb")
        x = self.st["sessions"]["teams-ask"]
        self.assertEqual(x["tools"], ["research@kit"])
        self.assertEqual([(r["tool"], r["state"]) for r in x["requests"]], [("mail", "needs-owner")])
        self.ev("tools.approved", "teams-ask", tool="mail", **OWNER)            # the owner's "yes" — as with an ordinary grant
        self.assertIn("mail", self.st["sessions"]["teams-ask"]["tools"])

    def test_a_base_tool_never_waits_for_the_owner(self):
        # Jira (atlassian) is in base, on every session; the hub also gave it to executors via --tools, and the
        # core asked for the owner's "yes" three times in a night for something the session already had
        self.ev("task.run_requested", "maps-fix", task="noindex", tools=["jira", "research@kit"], **by("hub"))
        self.start("maps-fix", raiser="hub", cwd="/sb")
        x = self.st["sessions"]["maps-fix"]
        self.assertEqual((x["tools"], x["requests"]), (["research@kit"], []))
        self.assertEqual(self.ev("tools.granted", "maps-fix", tool="jira", **by("hub")), "already has it: base")
        self.assertEqual(self.st["sessions"]["maps-fix"]["requests"], [])

    def test_a_risky_tool_with_the_owners_word_starts_with_it(self):
        self.ev("message.received", None, to="hub", text="ask a colleague by mail", **OWNER)
        mid = next(m["id"] for m in self.st["messages"].values() if m["session"] == "hub")
        self.ev("task.run_requested", "teams-ask", task="ask", tools=["mail"], **{"for": mid}, **by("hub"))
        self.start("teams-ask", raiser="hub", cwd="/sb")
        x = self.st["sessions"]["teams-ask"]
        self.assertEqual((x["tools"], x["requests"]), (["mail"], []))

    def test_the_owner_gives_risky_tools_himself(self):
        self.ev("task.run_requested", "teams-ask", task="ask", tools=["mail"], **OWNER)
        self.start("teams-ask", raiser=None, cwd="/sb")
        self.assertEqual(self.st["sessions"]["teams-ask"]["tools"], ["mail"])

    def test_a_one_at_a_time_tool_moves_from_its_holder(self):
        self.ev("tools.granted", "frontend", tool="browser:mac", **by("hub"))
        self.ev("task.run_requested", "shot", task="snapshot", tools=["browser:mac"], **by("hub"))
        self.start("shot", raiser="hub", cwd="/sb")
        self.assertEqual(self.st["sessions"]["shot"]["tools"], ["browser:mac"])
        self.assertNotIn("browser:mac", self.st["sessions"]["frontend"]["tools"])
        self.assertFalse(self.st["sessions"]["shot"]["dirty"])

    def test_a_risky_tool_the_executor_inherits_is_not_asked_for(self):
        # the executor inherited the tool of whoever raised it, and the same tool in --tools without the
        # owner's word left a "waiting for the owner's yes" request on something already present; the hub's grant
        # on the owner's "allow" was refused with "already has it"
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", role="agent", tools=["mail"], **OWNER)
        self.ev("session.started", "maps")
        self.ev("task.run_requested", "maps-check", task="check", tools=["mail"], **by("maps"))
        self.start("maps-check", raiser="maps", cwd="/m/x")
        x = self.st["sessions"]["maps-check"]
        self.assertEqual((x["tools"], x["requests"], x["dirty"]), (["mail"], [], False))

    def test_a_risky_tool_the_executors_repo_brings_is_not_asked_for(self):
        # the shape of the journal event: --tools browser:mac,conduit from a session; conduit is in the repo's .mcp.json,
        # the executor gets it from there at the start — the owner got a card for it all the same
        self.assertIsNone(self.ev("task.run_requested", "maps-job", task="release", tools=["browser:mac", "conduit"],
                                  **by("frontend")))
        self.start("maps-job", cwd="/fe/.claude/worktrees/maps-job", repo_tools=["conduit"], src="core")
        x = self.st["sessions"]["maps-job"]
        self.assertEqual([(r["tool"], r["state"]) for r in x["requests"]], [("conduit", "granted")])
        self.assertEqual((x["tools"], x["dirty"]), (["design@kit", "browser:mac"], False))   # nothing to attach or restart
        (d,) = [d for d in self.st["delegations"].values() if d["session"] == "maps-job"]
        self.assertIn("Already available", d["context"])                            # the executor is told
        self.assertIn("conduit", d["context"].split("Already available")[1].splitlines()[0])
        # nothing is waiting for the owner: no card, nothing to approve
        self.assertEqual(self.ev("tools.approved", "maps-job", tool="conduit", **OWNER), "nothing waits for the owner")
        # asked for again later (the session does not know where it comes from): no new request, no card
        self.assertEqual(self.ev("tools.requested", "maps-job", tool="conduit", why="need logs", **by("maps-job")),
                         "already has it: from its repo")
        self.assertEqual(self.ev("tools.granted", "maps-job", tool="conduit", **by("hub")),
                         "already has it: from its repo")
        self.assertEqual([(r["tool"], r["state"]) for r in x["requests"]], [("conduit", "granted")])

    def test_the_repo_does_not_open_another_risky_tool(self):
        # the gate stays: only what the daemon found in the repo skips the card, the rest asks as before
        self.ev("task.run_requested", "maps-job", task="release", tools=["mail", "conduit"], **by("frontend"))
        self.start("maps-job", repo_tools=["conduit"], src="core")
        x = self.st["sessions"]["maps-job"]
        self.assertEqual([(r["tool"], r["state"]) for r in x["requests"]],
                         [("mail", "needs-owner"), ("conduit", "granted")])

    def test_no_tool_in_the_repo_is_a_card_as_before(self):
        self.ev("task.run_requested", "maps-job", task="release", tools=["conduit"], **by("frontend"))
        self.start("maps-job", src="core")
        self.assertEqual([(r["tool"], r["state"]) for r in self.st["sessions"]["maps-job"]["requests"]],
                         [("conduit", "needs-owner")])

    def test_a_claim_of_the_repo_not_from_the_core_is_not_believed(self):
        # session.requested is also sent by the hub's `hub session new`: only the daemon's own event vouches for the disk
        self.ev("task.run_requested", "maps-job", task="release", tools=["conduit"], **by("frontend"))
        self.start("maps-job", repo_tools=["conduit"], src="session")
        self.assertEqual([(r["tool"], r["state"]) for r in self.st["sessions"]["maps-job"]["requests"]],
                         [("conduit", "needs-owner")])

    def stale(self):
        """The journal before the fix: the executor has the tool and a "waiting for the owner" request hangs on the same tool."""
        self.ev("task.run_requested", "teams-ask", task="ask", tools=["mail"], **by("hub"))
        self.start("teams-ask", raiser="hub", cwd="/sb")
        self.ev("session.started", "teams-ask")
        x = self.st["sessions"]["teams-ask"]
        x["tools"].append("mail")
        x["dirty"] = False
        return x

    def test_a_grant_of_a_tool_the_session_has_closes_its_waiting_request(self):
        self.stale()
        self.ev("message.received", None, to="hub", text="Allow", **OWNER)
        mid = next(m["id"] for m in self.st["messages"].values() if m["session"] == "hub")
        self.assertIsNone(self.ev("tools.granted", "teams-ask", tool="mail", **{"for": mid}, **by("hub")))
        x = self.st["sessions"]["teams-ask"]
        self.assertEqual((x["tools"], [(r["tool"], r["state"]) for r in x["requests"]], x["dirty"]),
                         (["mail"], [("mail", "granted")], False))       # nothing granted again — no restart
        self.assertEqual(x["requests"][0]["had"], self.seq)                 # the CLI of this grant will say "already connected"
        ev = {"seq": self.seq, "ts": 1000 + self.seq, "type": "tools.granted", "entity": "teams-ask",
              "data": {"tool": "mail", "for": mid}}
        self.assertEqual(views._items(self.st, ev)[0][1]["text"], "mail was already connected")   # not "connected" again
        self.assertEqual(self.ev("tools.granted", "teams-ask", tool="mail", **by("hub")), "already has it")

    def test_a_grant_without_the_owners_word_also_closes_it(self):
        self.stale()                                     # nothing to grant: withdrawing the request does not bypass the owner's "yes"
        self.assertIsNone(self.ev("tools.granted", "teams-ask", tool="mail", **by("hub")))
        self.assertEqual([r["state"] for r in self.st["sessions"]["teams-ask"]["requests"]], ["granted"])

    def test_the_owners_yes_on_a_tool_the_session_has_closes_it_without_a_second_copy(self):
        self.stale()
        self.assertIsNone(self.ev("tools.approved", "teams-ask", tool="mail", **OWNER))
        x = self.st["sessions"]["teams-ask"]
        self.assertEqual((x["tools"], [r["state"] for r in x["requests"]], x["dirty"]), (["mail"], ["granted"], False))
        ev = {"seq": self.seq, "ts": 1000 + self.seq, "type": "tools.approved", "entity": "teams-ask",
              "data": {"tool": "mail", **OWNER}}
        self.assertEqual(views._items(self.st, ev)[0][1]["text"], "you allowed mail — it was already connected")
        self.assertTrue([m for m in self.st["messages"].values() if m["session"] == "hub" and "there will be no restart" in m["text"]])

    def test_a_sleeping_or_parked_session_has_its_waiting_request_closed_without_a_wake(self):
        # the executor sleeps, browser is in the set, "waiting for the owner" hangs — the hub's grant was
        # refused with "no live session" (earlier "already has it"), and the CLI again said "waiting for the owner's
        # yes"; closing is possible in any state — no process is needed
        for kind, args, how in (("tools.granted", by("hub"), "surrendered"), ("tools.approved", OWNER, "surrendered"),
                                ("tools.granted", by("hub"), "parking")):
            with self.subTest(kind=kind, how=how):
                self.setUp()
                if how == "parking":   # a free agent: a "waiting for the owner" request, the tool is in the set (the journal before the fix)
                    name = "frontend"
                    self.assertIsNone(self.ev("tools.granted", name, tool="mail", **by("hub")))
                    self.st["sessions"][name]["tools"].append("mail")
                    self.assertIsNone(self.ev("session.parked", name, channel="core", why="idle"))
                else:
                    name = "teams-ask"
                    self.stale()
                    self.assertIsNone(self.ev("session.stop_requested", name, **by("hub")))
                x = self.st["sessions"][name]
                self.assertEqual(x["state"],
                                 "parked" if how == "parking" else "retired")
                self.assertEqual([r["state"] for r in x["requests"]], ["needs-owner"])
                acts = len(self.actions)
                self.assertIsNone(self.ev(kind, name, tool="mail", **args))
                x = self.st["sessions"][name]
                self.assertEqual((x["state"], x["tools"].count("mail"), [r["state"] for r in x["requests"]], x["dirty"]),
                                 ("parked" if how == "parking" else "retired", 1, ["granted"], False))     # no second copy and no restart
                self.assertEqual(x["requests"][0]["had"], self.seq)
                self.assertFalse([a for a in self.actions[acts:] if a.get("session") == name])  # does not wake it
                self.assertEqual(self.ev(kind, name, tool="mail", **args),
                                 "already has it" if kind == "tools.granted" else "nothing waits for the owner")

    def test_a_sleeping_session_without_the_tool_takes_only_the_owners_word(self):
        # the owner's "yes" to a sleeping session without the tool was lost ("no live session") and the card
        # hung. A hub grant to a sleeping session is still refused; the owner's "yes" and refusal are recorded at
        # once, without a start
        for kind in ("tools.approved", "tools.denied", "tools.granted"):   # the owner's grant is the same "yes"
            with self.subTest(kind=kind):
                self.setUp()
                x = self.stale()
                x["tools"].remove("mail")
                self.assertIsNone(self.ev("session.stop_requested", "teams-ask", **by("hub")))
                self.assertEqual(self.ev("tools.granted", "teams-ask", tool="mail", **by("hub")), "no live session")
                self.assertEqual(self.ev(kind, "teams-ask", tool="mail", **by("hub")),   # on the owner's behalf — no
                                 {"tools.approved": "only the owner approves", "tools.denied": "waits for the owner"}.get(
                                     kind, "no live session"))
                x = self.st["sessions"]["teams-ask"]
                self.assertEqual([r["state"] for r in x["requests"]], ["needs-owner"])
                acts, msgs = len(self.actions), len(self.st["messages"])
                self.assertIsNone(self.ev(kind, "teams-ask", tool="mail", **OWNER))
                x = self.st["sessions"]["teams-ask"]
                self.assertEqual((x["state"], x["tools"].count("mail"), [r["state"] for r in x["requests"]]),
                                 ("retired", int(kind != "tools.denied"),
                                  ["denied" if kind == "tools.denied" else "granted"]))
                self.assertNotIn("new_tools", x)
                self.assertFalse([a for a in self.actions[acts:] if a.get("session") == "teams-ask"])  # does not wake it
                self.assertFalse([m for m in list(self.st["messages"].values())[msgs:] if m["session"] == "teams-ask"])
        self.setUp()   # the owner's word to the hub (--for mN) is the same "yes", for a sleeping session too
        x = self.stale()
        x["tools"].remove("mail")
        self.assertIsNone(self.ev("session.stop_requested", "teams-ask", **by("hub")))
        self.ev("message.received", None, to="hub", text="Allow mail", **OWNER)
        mid = next(m["id"] for m in self.st["messages"].values() if m["text"] == "Allow mail")
        self.assertIsNone(self.ev("tools.granted", "teams-ask", tool="mail", **{"for": mid}, **by("hub")))
        x = self.st["sessions"]["teams-ask"]
        self.assertEqual((x["state"], x["tools"], x["requests"][0]["state"], x["requests"][0]["owner_words"]["message"]),
                         ("retired", ["mail"], "granted", mid))
        self.setUp()
        x = self.stale()   # a closed one — as before: it will not return, there is no one to grant to
        x["tools"].remove("mail")
        x["state"] = "closed"
        self.assertEqual(self.ev("tools.approved", "teams-ask", tool="mail", **OWNER), "no live session")
        self.assertEqual(self.ev("tools.denied", "teams-ask", tool="mail", **OWNER), "no live session")


    def test_a_grant_to_a_sleeping_session_waits_for_its_wake(self):
        # a hub grant to a sleeping or parked session was refused with "no live session" ("grant again when it
        # is up"); waking it for a grant is not allowed. The grant is recorded in the set, and the start (spawn) takes
        # the set whole
        for how in ("asleep", "parked"):
            with self.subTest(how=how):
                self.setUp()
                if how == "parked":
                    self.assertIsNone(self.ev("session.parked", "frontend", channel="core", why="idle"))
                else:
                    self.st["sessions"]["frontend"]["state"] = "asleep"
                acts = len(self.actions)
                self.assertIsNone(self.ev("tools.granted", "frontend", tool="research@kit", **by("hub")))
                x = self.st["sessions"]["frontend"]
                self.assertEqual((x["state"], x["tools"].count("research@kit")), (how, 1))
                self.assertEqual(self.ev("tools.granted", "frontend", tool="research@kit", **by("hub")),
                                 "already has it")                                       # no second copy
                self.assertEqual(self.st["sessions"]["frontend"]["tools"].count("research@kit"), 1)
                self.assertFalse([a for a in self.actions[acts:] if a.get("session") == "frontend"])  # does not wake it
                self.ev("message.received", None, to="frontend", text="go on", **OWNER)
                (spawn,) = self.spawn_of("frontend")[-1:]
                self.assertIn("research@kit", spawn["tools"])
                self.ev("session.started", "frontend")
                self.assertFalse(self.st["sessions"]["frontend"]["dirty"])   # started with it — no second start
                self.assertTrue([m for m in self.st["messages"].values() if m["session"] == "frontend"
                                 and "research@kit" in m["text"] and "it is already in your set" in m["text"]])

    def test_a_revoke_before_the_wake_cancels_the_sleeping_grant(self):
        self.ev("session.parked", "frontend", channel="core", why="idle")
        self.assertIsNone(self.ev("tools.granted", "frontend", tool="research@kit", **by("hub")))
        self.assertIsNone(self.ev("tools.revoked", "frontend", tool="research@kit", **by("hub")))
        self.ev("message.received", None, to="frontend", text="go on", **OWNER)
        (spawn,) = self.spawn_of("frontend")[-1:]
        self.assertNotIn("research@kit", spawn["tools"])

    def test_a_sleeping_session_gets_a_risky_tool_only_with_the_owners_word(self):
        self.ev("session.parked", "frontend", channel="core", why="idle")
        self.assertIsNone(self.ev("tools.granted", "frontend", tool="mail", **by("hub")))
        x = self.st["sessions"]["frontend"]
        self.assertEqual((x["tools"].count("mail"), [r["state"] for r in x["requests"]]), (0, ["needs-owner"]))
        self.assertIsNone(self.ev("tools.approved", "frontend", tool="mail", **OWNER))
        x = self.st["sessions"]["frontend"]
        self.assertEqual((x["state"], x["tools"].count("mail")), ("parked", 1))

    def test_a_retired_executor_still_takes_no_grant(self):
        self.stale()["tools"].remove("mail")
        self.assertIsNone(self.ev("session.stop_requested", "teams-ask", **by("hub")))
        self.assertEqual(self.ev("tools.granted", "teams-ask", tool="research@kit", **by("hub")), "no live session")

    def test_an_exclusive_tool_is_not_taken_from_its_holder_for_a_sleeping_one(self):
        # one for all: granting it to a sleeping session would take it from a live holder, and the sleeping one may not wake for days
        self.ev("session.requested", "seo", cwd="/seo", session_id="s-seo", **OWNER)
        self.ev("session.started", "seo")
        self.assertIsNone(self.ev("tools.granted", "seo", tool="browser:mac", **by("hub")))
        self.ev("session.parked", "frontend", channel="core", why="idle")
        self.assertIn("one for all", self.ev("tools.granted", "frontend", tool="browser:mac", **by("hub")))
        self.assertEqual((self.st["sessions"]["seo"]["tools"], self.st["sessions"]["frontend"]["tools"].count(
            "browser:mac")), (["browser:mac"], 0))
        self.st["sessions"]["seo"]["state"] = "asleep"   # the holder sleeps too — there is no working one to take it from
        self.assertIsNone(self.ev("tools.granted", "frontend", tool="browser:mac", **by("hub")))
        self.assertEqual(self.st["sessions"]["frontend"]["tools"].count("browser:mac"), 1)

    def test_the_sleeping_ones_note_names_who_granted(self):
        # a non-risky tool by the hub gets the hub-connected note, even with --for (the owner's word is not checked for a non-risky one)
        self.ev("session.parked", "frontend", channel="core", why="idle")
        self.assertIsNone(self.ev("tools.granted", "frontend", tool="research@kit", **by("hub"), **{"for": "m999999"}))
        self.assertIsNone(self.ev("tools.granted", "frontend", tool="mail", **OWNER))
        notes = [m["text"] for m in self.st["messages"].values() if m["session"] == "frontend" and m.get("fyi")]
        self.assertTrue(notes[-2].startswith("hub connected research@kit"), notes)
        self.assertTrue(notes[-1].startswith("The owner connected mail"), notes)


class GrantNote(unittest.TestCase):
    """The hint after `hub tools grant` (PR #25): a busy session is told honestly that the tool will not be there in this turn."""

    def test_a_busy_session_is_told_the_tool_comes_after_the_turn(self):
        for state in ("busy", "dialog", "starting"):
            note = tools.grant_note(state)
            self.assertEqual(note, i18n.t("core.tools.grant_busy"), state)   # the CLI's language (English in tests)
            self.assertIn("will not be there in this turn", note, state)
            self.assertIn("--tools", note, state)                            # actionable advice, not "grant before run"

    def test_a_free_session_keeps_the_old_wording(self):
        self.assertEqual(tools.grant_note("idle"), i18n.t("core.tools.grant_free"))   # the CLI's language
        self.assertEqual(tools.grant_note(None), tools.grant_note("idle"))


if __name__ == "__main__":
    unittest.main()


class DropTools(unittest.TestCase):
    """hub.json executors.agents.<agent>.drop_tools: tools an agent's executors do not inherit from it (off by default; what
    the task names with --tools stays)."""
    setUp, ev, run_task, start, spawn_of = (TaskRunTools.__dict__[n] for n in ("setUp", "ev", "run_task", "start", "spawn_of"))

    def drop(self, **drops):
        self.assertIsNone(self.ev("owner.settings", None, timezone="UTC", drop_tools=drops, channel="core"))

    def tools_of(self, name):
        self.start(name)
        return self.st["sessions"][name]["tools"]

    def test_the_normalizer_keeps_lists_of_names_and_ignores_the_rest(self):
        cfg = {"executors": {"agents": {"frontend": {"drop_tools": ["mail", "mail", " ", 7, "research@kit"]},
                                        "seo": {"drop_tools": "mail"}, "docs": {"drop_tools": []}, "maps": ["mail"],
                                        "hub": {"model": "opus"}, 3: {"drop_tools": ["x"]}}}}
        self.assertEqual(executors.drop_tools_from(cfg), {"frontend": ["mail", "research@kit"]})
        for bad in (None, {}, {"executors": None}, {"executors": {"agents": None}}, {"executors": {"agents": ["a"]}}):
            self.assertEqual(executors.drop_tools_from(bad), {})

    def test_without_the_option_the_executor_inherits_all(self):
        self.run_task("frontend-ci")
        self.assertEqual(self.tools_of("frontend-ci"), ["design@kit"])

    def test_the_named_tools_are_not_copied_to_the_executor_but_stay_with_the_raiser(self):
        self.drop(frontend=["design@kit"])
        self.run_task("frontend-ci")
        self.assertEqual(self.tools_of("frontend-ci"), [])
        (spawn,) = self.spawn_of("frontend-ci")
        self.assertEqual(spawn["tools"], [])
        self.assertEqual(self.st["sessions"]["frontend"]["tools"], ["design@kit"])

    def test_what_the_task_names_with_tools_stays(self):
        self.ev("catalog.loaded", tools={**CATALOG, "design@kit": {"kind": "plugin", "risk": []}})
        self.drop(frontend=["design@kit"])
        self.run_task("frontend-ci", tools=["design@kit"])
        self.assertEqual(self.tools_of("frontend-ci"), ["design@kit"])

    def test_another_agents_executors_and_the_hubs_are_not_touched(self):
        self.drop(seo=["design@kit"])
        self.run_task("frontend-ci")
        self.assertEqual(self.tools_of("frontend-ci"), ["design@kit"])


class DropToolsDaemon(unittest.TestCase):
    def test_the_daemon_writes_the_setting_as_a_fact_when_it_changes_and_only_then(self):
        import json
        import os
        import tempfile
        from core import catalog, journal
        from core.daemon import Hub
        h = Hub(tempfile.mkdtemp(prefix="drop-d-"), type("H", (), {"alive": lambda s, n: True, "poll": lambda s: []})(),
                registry_dir=tempfile.mkdtemp())
        h.execute = lambda a: None

        def facts():
            return [e["data"].get("drop_tools") for e in journal.read_tail(os.path.join(h.state_dir, "journal.jsonl"))
                    if e["type"] == "owner.settings"]

        def write(agents):
            with open(catalog.config_path(h.state_dir), "w") as f:
                json.dump({"executors": {"agents": agents}}, f)
            h.load_catalog()
        h.load_catalog()
        self.assertEqual(facts(), [None])                                  # off by default: not in the fact at all
        write({"seo": {"drop_tools": ["mail"]}})
        self.assertEqual(h.state["owner"]["drop_tools"], {"seo": ["mail"]})
        write({"seo": {"drop_tools": ["mail"]}})
        self.assertEqual(len(facts()), 2)                                  # unchanged: no new fact
        write({"seo": {"drop_tools": "mail"}})                             # a wrong shape is ignored, the option is off
        self.assertEqual(h.state["owner"]["drop_tools"], {})
