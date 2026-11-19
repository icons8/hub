"""`hub task` — tasks on top of delegations (the "tasks, specialists, executors" spec, release 1): any session can
set a task, and the result returns to it; readiness is set by whoever set the task."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

import platforms
from core import journal, spool
from tests.fakes import lang

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")


class TaskCli(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="task-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for n in ("maps", "devops"):
            j.append("session.requested", n, {"cwd": "/w", "session_id": f"s-{n}", "channel": "cli", "owner": True})

    def hub(self, *args, me=None):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"}
        env.pop("HUB_SESSION", None)
        if me:
            env["HUB_SESSION"] = me
        r = subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return [ev for _, ev in spool.take(os.path.join(self.state, "spool"))]

    def test_a_session_sets_a_task_and_the_result_comes_back_to_it(self):
        (ev,) = self.hub("task", "new", "devops", "raise the worker memory limit to 2 GB", me="maps")
        self.assertEqual(ev["type"], "delegation.created")
        self.assertEqual((ev["data"]["session"], ev["data"]["for_session"], ev["data"]["context"]),
                         ("devops", "maps", "raise the worker memory limit to 2 GB"))

    def test_run_raises_an_executor(self):
        (ev,) = self.hub("task", "run", "devops-limits", "raise the memory limit", "--repo", self.state, "--work", "normal", me="devops")
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["task"], ev["data"]["repo"]),
                         ("task.run_requested", "devops-limits", "raise the memory limit", os.path.realpath(self.state)
                          if False else self.state))

    def test_run_of_a_direction_without_repo_takes_the_directions_repo(self):
        # the hub created a direction and brought it up with hub task run <slug>, without --repo: the session came up in
        # the hub's own folder (not git), worked by itself and could not become a mini-hub. The direction knows its repo.
        dirs = tempfile.mkdtemp(prefix="dirs-")
        repo = tempfile.mkdtemp(prefix="maps2-repo-")
        for d in (dirs, repo):
            self.addCleanup(platforms.current.rmtree, d)
        os.makedirs(os.path.join(dirs, "maps2", "backlog"))
        with open(os.path.join(dirs, "maps2", "direction.json"), "w", encoding="utf-8") as f:
            f.write('{"repo": %s}' % json.dumps(repo))
        os.environ["HUB_DIRECTIONS"], was = dirs, os.environ.get("HUB_DIRECTIONS")
        try:
            (ev,) = self.hub("task", "run", "maps2", "look into the epic", "--work", "risky", me="devops")
            self.assertEqual((ev["entity"], ev["data"].get("repo")), ("maps2", repo))
            (ev,) = [e for e in self.hub("task", "run", "maps2", "look", "--repo", self.state, "--work", "risky",
                                         me="devops") if e["data"].get("task") == "look"]
            self.assertEqual(ev["data"]["repo"], self.state)                     # an explicit --repo wins
            (ev,) = [e for e in self.hub("task", "run", "maps2", "on it", "--branch", "feat/x", "--work", "risky",
                                         me="devops") if e["data"].get("task") == "on it"]
            self.assertEqual(ev["data"].get("repo"), repo)                       # the branch is in its repo
            (ev,) = [e for e in self.hub("task", "run", "maps2", "here", "--here", "--work", "risky", me="devops")
                     if e["data"].get("task") == "here"]
            self.assertNotIn("repo", ev["data"])                                 # --here names its copy itself
            (ev,) = [e for e in self.hub("task", "run", "maps3", "no such direction", "--work", "normal", me="devops")
                     if e["entity"] == "maps3"]
            self.assertNotIn("repo", ev["data"])                                 # not a direction: the raiser's folder
        finally:
            os.environ.pop("HUB_DIRECTIONS")
            if was is not None:
                os.environ["HUB_DIRECTIONS"] = was

    def test_run_on_an_existing_branch_or_right_in_a_ready_copy(self):
        (ev,) = self.hub("task", "run", "devops-mr", "finish the MR", "--branch", "feat/limits", "--work", "normal",
                         me="devops")
        self.assertEqual((ev["data"]["branch"], ev["data"].get("here"), ev["data"]["task"]),
                         ("feat/limits", None, "finish the MR"))
        (ev,) = [e for e in self.hub("task", "run", "devops-w", "finish", "--repo", self.state, "--here", "--work",
                                     "normal", me="devops") if e["entity"] == "devops-w"]
        self.assertEqual((ev["data"]["here"], ev["data"]["repo"], ev["data"]["task"]), (True, self.state, "finish"))
        (ev,) = [e for e in self.hub("task", "run", "devops-m", "fix", "--from-main", "--work", "normal",
                                     me="devops") if e["entity"] == "devops-m"]
        self.assertEqual((ev["data"]["from_main"], ev["data"]["task"]), (True, "fix"))

    def test_revive_keeps_the_branch_of_an_executor_on_an_existing_branch(self):
        # a --branch copy in place: revive carries keep_branch, otherwise the next cleanup would delete the branch
        # (git branch -d)
        copy = tempfile.mkdtemp(prefix="copy-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        own = {"channel": "cli", "owner": True}
        wt = {"path": copy, "branch": "feat/x", "repo": "/w", "keep_branch": True}
        j.append("session.requested", "maps-x", {"cwd": copy, "session_id": "s-x", "executor": True, "raised_by": "maps",
                                                 "worktree": wt, **own})
        j.append("session.started", "maps-x", {})
        j.append("session.stop_requested", "maps-x", own)
        j.append("session.remove_requested", "maps-x", own)
        (ev,) = [e for e in self.hub("session", "revive", "maps-x") if e["entity"] == "maps-x"]
        self.assertEqual(ev["data"]["worktree"], wt)

    def test_revive_keeps_the_from_main_choice_of_an_executor(self):
        # PR #283: a copy created with --from-main — revive carries the choice in its details, restore takes the same
        # point
        copy = tempfile.mkdtemp(prefix="copy-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        own = {"channel": "cli", "owner": True}
        wt = {"path": copy, "branch": "task/maps-m", "repo": "/w", "from_main": True}
        j.append("session.requested", "maps-m", {"cwd": copy, "session_id": "s-m", "executor": True, "raised_by": "maps",
                                                 "worktree": wt, **own})
        j.append("session.started", "maps-m", {})
        j.append("session.stop_requested", "maps-m", own)
        j.append("session.remove_requested", "maps-m", own)
        (ev,) = [e for e in self.hub("session", "revive", "maps-m") if e["entity"] == "maps-m"]
        self.assertEqual(ev["data"]["worktree"], wt)

    def test_fork_is_gone(self):
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"}
        r = subprocess.run([sys.executable, HUB, "session", "fork", "devops"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)   # no such command (there are no forks): help is shown, nothing reached the core

    def test_the_hub_sets_a_task_with_the_owners_words_or_for_a_session(self):
        # decision of 2026-10-05: delegation and task are the same thing, keep one — "task": hub delegate is gone
        (ev,) = self.hub("task", "new", "devops", "--for", "m7", "context", me="hub")
        self.assertEqual((ev["data"]["session"], ev["data"]["for"], ev["data"]["for_session"], ev["data"]["context"]),
                         ("devops", "m7", None, "context"))   # the result goes to the owner under their words, as with the old delegate
        ev = self.hub("task", "new", "devops", "--for-session", "maps", "a new session is needed", me="hub")[-1]
        self.assertEqual((ev["data"]["for_session"], ev["data"]["context"]), ("maps", "a new session is needed"))
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0"}
        for old in (["delegate", "devops", "x"], ["delegations"], ["delegation", "done", "d1"]):
            r = subprocess.run([sys.executable, HUB, *old], env=env, capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0, old)
            self.assertIn("hub task", r.stderr, old)

    def test_route_a_message_for_a_busy_specialist(self):
        (ev,) = self.hub("route", "d7", "executor", "--work", "risky", me="hub")
        self.assertEqual((ev["type"], ev["entity"], ev["data"]["route"]), ("message.routed", "d7", "executor"))
        ev = self.hub("route", "d9", "executor", "--name", "model", "--work", "normal", me="hub")[-1]
        self.assertEqual(ev["data"]["name"], "model")
        ev = self.hub("route", "m8", "drop", "already", "done", me="hub")[-1]
        self.assertEqual(ev["data"]["why"], "already done")
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "hub"}
        r = subprocess.run([sys.executable, HUB, "route", "m8", "drop"], env=env, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("the reason", r.stderr)   # the hub's command: in English

    def test_waiting_without_a_check_warns(self):
        # decision of 2026-10-05: "waiting" for a person or a service without a check — nobody will catch the reply
        env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CLI_WAIT": "0", "HUB_SESSION": "maps"}
        run = lambda *a: subprocess.run([sys.executable, HUB, *a], env=env, capture_output=True, text=True)
        r = run("waiting", "a reply from support to request 42 in the mail")
        self.assertEqual(r.returncode, 0)
        self.assertIn("nothing checks it", r.stdout)   # a session's command: in English
        self.assertIn("hub wait", r.stdout)
        for who in ("a reply from devops about the limit", lang.word("words", "people_stems"), "the hub"):   # a session or the owner — the reply will come by itself
            self.assertNotIn("nothing checks it", run("waiting", who).stdout, who)
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("schedule.set", "maps-wait-ab-1", {"session": "maps", "at": time.time() + 600, "prompt": "check",
                                                    "channel": "session", "owner": False, "sender": "maps"})
        self.assertNotIn("nothing checks it", run("waiting", "a reply from support in the mail").stdout)   # a check is in place

    def test_done_closes_the_task(self):
        (ev,) = self.hub("task", "done", "d42", me="maps")
        self.assertEqual((ev["type"], ev["entity"]), ("delegation.done", "d42"))


if __name__ == "__main__":
    unittest.main()
