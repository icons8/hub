"""A session's own number of executors (spec "parallelism per session"): for a specialist and a direction — the
default (the hub's overall parallelism N), "itself" (0) or its own number ≥ 1. "Itself" is the earlier solo field and
the event session.solo_set (survives a rollback to the old core), its own number is the parallel field and the event
session.parallel_set. On top — "itself" by folder (solo_auto) and the overall ceiling max_live. Set by the owner and
the hub (or the dev session); not by a session for itself."""
import tests  # noqa: F401 — do not see the machine's settings
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from core import executors, i18n, journal, minihub, spool
from tests.test_minihub import OWNER, Delegating, by

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Own(Delegating):
    def own(self, name, n, who=None):
        self.actions.clear()
        live = self.live()
        why = self.ev("session.parallel_set", name, n=n, **(by(who) if who else OWNER))
        self.respawned()
        self.unpark(live)
        return why

    def sess(self, name="notes"):
        return self.st["sessions"][name]


class Limit(Own):
    """The effective number: "itself" → 0, own → it, else the hub's N; a journal from before parallelism — None (the
    old rules)."""

    def test_matrix(self):
        notes = lambda: self.sess()
        self.assertIsNone(minihub.limit(self.st, notes()))                    # journal from before parallelism
        self.parallel(3)
        self.assertEqual(minihub.limit(self.st, notes()), 3)
        self.assertIsNone(self.own("notes", 2))
        self.assertEqual(minihub.limit(self.st, notes()), 2)
        self.parallel(0)
        self.assertEqual(minihub.limit(self.st, notes()), 2)                  # own beats the overall, and zero too
        self.assertIsNone(self.own("notes", "default"))
        self.assertEqual(minihub.limit(self.st, notes()), 0)
        self.solo("notes")
        self.parallel(5)
        self.assertEqual(minihub.limit(self.st, notes()), 0)
        self.assertIsNone(minihub.limit(self.st, self.st["sessions"]["docs"]))   # not a specialist

    def test_own_number_caps_the_runs(self):
        self.minihub(3)
        self.assertIsNone(self.own("notes", 1))
        self.assertIsNone(self.run_task("notes-a"))
        self.assertIsNone(self.run_task("notes-b"))
        self.assertEqual((self.state_of("notes-a"), self.state_of("notes-b")), ("ready", "queued"))
        self.assertIn("1 working executors", self.st["runs"]["notes-b"]["why"])
        self.assertIsNone(self.run_task("api-x-a", who="api-x"))             # the neighbor has the shared 3
        self.assertIsNone(self.run_task("api-x-b", who="api-x"))
        self.assertEqual((self.state_of("api-x-a"), self.state_of("api-x-b")), ("ready", "ready"))

    def test_raising_lets_the_queue_rise_and_lowering_keeps_the_ready(self):
        self.minihub(3)
        self.own("notes", 1)
        self.run_task("notes-a")
        self.run_task("notes-b")
        self.assertEqual(executors.promotable(self.st), [])
        self.own("notes", 2)
        self.assertEqual(executors.promotable(self.st), ["notes-b"])
        self.own("notes", 1)                                                  # a ready one passed the check — not cleared
        self.assertEqual(self.state_of("notes-a"), "ready")

    def test_own_number_on_a_hub_of_zero_makes_a_minihub(self):
        self.parallel(0)
        self.assertIsNotNone(minihub.alone(self.st, self.sess()))
        self.own("notes", 2)
        self.folder("notes")                                                  # daemon: a git folder, no task
        self.assertTrue(minihub.on(self.st, self.sess()))
        self.assertIsNone(minihub.alone(self.st, self.sess()))
        self.assertIsNone(self.run_task("notes-a"))
        self.assertEqual(self.state_of("notes-a"), "ready")

    def test_hub_wide_cap_stays_on_top(self):
        self.ev("owner.settings", None, timezone="UTC", parallel=3, executors={"max_live": 1}, channel="core")
        self.own("notes", 5)
        self.run_task("notes-a")
        self.start("notes-a")
        self.run_task("notes-b")
        self.assertEqual(self.state_of("notes-b"), "queued")
        self.assertIn("the hub's ceiling", self.st["runs"]["notes-b"]["why"])

    def test_folder_without_git_wins_and_the_number_waits_for_it(self):
        self.parallel(3)
        self.folder("notes", git="none")
        self.assertIsNone(self.own("notes", 4))                               # the write goes through
        self.assertEqual(self.sess()["parallel"], 4)
        self.assertFalse(minihub.want(self.st, self.sess()))
        self.assertIn("the folder is not git", minihub.alone(self.st, self.sess()))
        self.folder("notes")
        self.assertTrue(minihub.want(self.st, self.sess()))


class Fields(Own):
    """One of three: solo, parallel ≥ 1, nothing. "Itself" is the earlier field: a rollback to the old core does not
    lose it."""

    def test_zero_is_solo_and_a_number_clears_it(self):
        self.parallel(3)
        self.assertIsNone(self.own("notes", 0))
        self.assertEqual((self.sess().get("solo"), "parallel" in self.sess()), (True, False))
        self.own("notes", 4)
        self.assertEqual((self.sess().get("solo"), self.sess().get("parallel")), (None, 4))
        self.solo("notes")
        self.assertEqual((self.sess().get("solo"), "parallel" in self.sess()), (True, False))
        self.own("notes", "default")
        self.assertEqual(("solo" in self.sess(), "parallel" in self.sess()), (False, False))

    def test_solo_off_keeps_an_own_number(self):
        self.parallel(3)
        self.own("notes", 4)
        self.assertIsNone(self.solo("notes", on=False))
        self.assertEqual(self.sess().get("parallel"), 4)

    def test_zero_drops_the_queue_like_solo(self):
        self.minihub(1)
        self.run_task("notes-a")
        self.run_task("notes-b")
        self.own("notes", 0)
        self.assertEqual(self.st.get("runs") or {}, {})
        (line,) = [m for m in self.to("notes") if "Executor requests are dropped" in m["text"]]
        self.assertIn("\"solo\" is chosen", line["text"])
        self.assertIn("\"solo\" is chosen", self.run_task("notes-c"))

    def test_turning_into_solo_leaves_minihub_at_once(self):
        self.minihub(3)
        self.assertTrue(minihub.on(self.st, self.sess()))
        self.own("notes", 0)
        self.assertFalse(minihub.on(self.st, self.sess()))
        self.assertNotIn("minihub_since", self.sess())

    def test_old_solo_reads_as_zero(self):
        self.parallel(3)
        self.sess()["solo"] = True                                            # the old core's state
        self.assertEqual(minihub.own(self.sess()), 0)
        self.assertEqual(minihub.limit(self.st, self.sess()), 0)

    def test_revive_and_a_system_agent_keep_the_number(self):
        from unittest import mock
        from core import layout
        self.parallel(3)
        self.own("notes", 4)
        self.ev("session.remove_requested", "notes", **OWNER)
        self.ev("session.requested", "notes", cwd="/notes", session_id="s-notes", revive=True, **OWNER)
        self.assertEqual(self.sess().get("parallel"), 4)
        self.own("api-x", 2)
        self.ev("session.remove_requested", "api-x", **OWNER)
        with mock.patch.object(layout, "keeps_place", lambda st, s: s["name"] == "api-x"):
            self.ev("session.requested", "api-x", cwd="/api-x", session_id="s-x2", role="agent", about="X", **OWNER)
        self.assertEqual(self.sess("api-x").get("parallel"), 2)


class Rights(Own):
    def test_owner_hub_and_trainer_only_not_itself(self):
        self.parallel(3)
        self.ev("agents.system", None, names=["dev", "trainer"], trainer="trainer", channel="core")
        self.assertIn("the owner, the hub or agent-dev", self.own("notes", 5, who="notes"))   # not by itself
        self.assertIn("the owner, the hub or agent-dev", self.own("notes", 5, who="api-x"))
        self.assertNotIn("parallel", self.sess())
        self.assertIsNone(self.own("notes", 2, who="hub"))
        self.assertIsNone(self.own("notes", 0, who="trainer"))
        self.assertIsNone(self.own("notes", 3))

    def test_only_a_specialist_or_a_direction(self):
        self.parallel(3)
        self.assertIn("for a specialist or a direction", self.own("docs", 2))
        self.assertIn("for a specialist or a direction", self.own("hub", 2))
        self.assertIn("unknown session", self.own("ghost", 2))

    def test_values(self):
        self.parallel(3)
        for bad in (-1, 1.5, 2.0, float("inf"), "2", True, None, "", "solo"):
            self.assertIn("number of executors", self.own("notes", bad) or "", bad)
        self.assertNotIn("parallel", self.sess())
        self.assertIsNone(self.own("notes", 20))                              # up to 20 — as in the "Parallelism" field
        self.assertEqual(self.sess()["parallel"], 20)
        self.assertIn("from 0 to 20", self.own("notes", 21))
        self.assertEqual(self.sess()["parallel"], 20)

    def test_refused_before_parallel_exists(self):
        self.assertIn("the hub has no parallelism yet", self.own("notes", 2))   # journal from before parallelism
        self.assertNotIn("parallel", self.sess())


class Cli(Own):
    """hub session parallel <name> <N|default>: 0 — the earlier solo_set (rollback), a number and default —
    parallel_set; hub session solo is a synonym; hub status says what is in effect."""

    def hub(self, *args, me=None):
        tmp = tempfile.mkdtemp(prefix="par-cli-")
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "journal.jsonl"), "w", encoding="utf-8") as f:
            for e in self.events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        env = {**os.environ, "HUB_STATE_DIR": tmp, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        if me:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, os.path.join(ROOT, "bin", "hub"), *args], env=env, capture_output=True,
                           text=True)
        return r, [(e["type"], e["entity"], {k: e["data"][k] for k in ("n", "on") if k in e["data"]})
                   for _, e in spool.take(os.path.join(tmp, "spool"))]

    def test_command(self):
        self.parallel(3)
        for arg, want in (("0", ("session.solo_set", "notes", {"on": True})),
                          ("4", ("session.parallel_set", "notes", {"n": 4})),
                          ("default", ("session.parallel_set", "notes", {"n": "default"}))):
            r, got = self.hub("session", "parallel", "notes", arg)
            self.assertEqual((r.returncode, got), (0, [want]), r.stderr)
        for arg in ("-1", "2.5", "many", "21", "²", "9" * 5000):
            r, got = self.hub("session", "parallel", "notes", arg)
            self.assertNotEqual(r.returncode, 0, arg)
            self.assertEqual(got, [])
        r, got = self.hub("session", "solo", "notes", "on")                   # the synonym remains
        self.assertEqual(got, [("session.solo_set", "notes", {"on": True})])

    def test_a_number_says_when_the_folder_still_keeps_it_solo(self):
        # 06.11: the hub set a direction's number on the owner's "become a mini-hub", the core said only "accepted", the
        # direction stayed solo (its folder is not git) and nobody learned why. The command says it and how to fix it
        self.parallel(3)
        self.folder("notes", git="none")
        r, got = self.hub("session", "parallel", "notes", "4")
        self.assertEqual((r.returncode, got), (0, [("session.parallel_set", "notes", {"n": 4})]), r.stderr)
        self.assertIn("hub session cwd notes", r.stdout)
        self.assertIn("git", r.stdout)
        r, _ = self.hub("session", "parallel", "notes", "0")                   # "solo" by choice: nothing to add
        self.assertNotIn("hub session cwd", r.stdout)
        self.folder("notes", git="empty")                                       # a repo with no commits: a commit
        r, _ = self.hub("session", "parallel", "notes", "4")                   # in place, no move
        self.assertIn("commit --allow-empty", r.stdout)
        self.assertNotIn("hub session cwd", r.stdout)
        self.folder("notes", git="ok")
        r, _ = self.hub("session", "parallel", "notes", "4")                   # a git folder: nothing to add
        self.assertNotIn("hub session cwd", r.stdout)

    def test_status_says_what_works(self):
        self.parallel(3)
        self.own("notes", 2)
        self.own("api-x", 0)
        r, _ = self.hub("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("up to 2 executors (its own number)", r.stdout)
        # the owner's terminal: the reason in his language (the core's phrase rendered where it is printed)
        self.assertIn(i18n.t("cli.status.solo_why", why=i18n.t("core.minihub.solo_chosen")), r.stdout)


class Web(unittest.TestCase):
    """The snapshot and the /api/session/parallel handle: 0 — the earlier solo_set, a number and default —
    parallel_set; nothing else."""

    def setUp(self):
        from modules.web.server import Web as W
        self.state = tempfile.mkdtemp(prefix="par-web-")
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.web = W(self.state)
        self.web.decide_wait = 0
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.state, ignore_errors=True)

    def req(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/json"})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def spooled(self):
        out = []
        for path, e in spool.take(os.path.join(self.state, "spool")):
            out.append((e["type"], e["entity"], {k: e["data"][k] for k in ("n", "on") if k in e["data"]}))
            os.unlink(path)
        return out

    def seed(self):
        a = {"channel": "cli", "owner": True}
        self.j.append("owner.settings", None, {"timezone": "UTC", "parallel": 3, "channel": "core"})
        for name in ("notes", "api-x"):
            self.j.append("session.requested", name, {"cwd": f"/{name}", "session_id": f"s-{name}", "role": "agent",
                                                      "about": name, **a})
            self.j.append("session.started", name, {})
        self.j.append("session.requested", "docs", {"cwd": "/docs", "session_id": "s-docs", **a})
        self.j.append("session.started", "docs", {})
        self.j.append("session.parallel_set", "notes", {"n": 2, **a})
        self.j.append("session.solo_set", "api-x", {"on": True, **a})
        self.j.append("session.requested", "notes-a", {"cwd": "/w", "session_id": "s-na", "executor": True,
                                                       "raised_by": "notes"})
        self.j.append("session.started", "notes-a", {})
        self.web.tail.poll()

    def test_snapshot(self):
        self.seed()
        snap = json.loads(self.req("GET", "/api/snapshot")[1])
        s = {x["name"]: x for x in snap["sessions"]}
        self.assertEqual(snap["parallel"], 3)
        self.assertEqual({k: s["notes"][k] for k in ("parallel", "solo", "executors_live")},
                         {"parallel": 2, "solo": False, "executors_live": 1})
        self.assertEqual({k: s["api-x"][k] for k in ("parallel", "solo")}, {"parallel": 0, "solo": True})
        self.assertIsNone(s["docs"]["parallel"])

    def test_handle(self):
        self.seed()
        for n, want in ((0, ("session.solo_set", "notes", {"on": True})), (4, ("session.parallel_set", "notes", {"n": 4})),
                        ("default", ("session.parallel_set", "notes", {"n": "default"}))):
            self.assertEqual(self.req("POST", "/api/session/parallel", {"session": "notes", "n": n})[0], 202, n)
            self.assertEqual(self.spooled(), [want])
        for n in (-1, 1.5, "3", True, None, 21):
            code, out = self.req("POST", "/api/session/parallel", {"session": "notes", "n": n})
            self.assertEqual(code, 400, n)
            self.assertTrue(json.loads(out)["error"])
        self.assertEqual(self.spooled(), [])


if __name__ == "__main__":
    unittest.main()
