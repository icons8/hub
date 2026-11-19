import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from core import journal, redact
from hooks import hub_hook
from hooks.hub_hook import translate

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "hub_hook.py")
RULE = "Invariant 7: the schema of the orders table changes only through a migration with a rollback"
OWNER = {"channel": "cli", "owner": True}


class HookTest(unittest.TestCase):
    def test_marker_with_and_without_sender(self):
        """Failure: a marker with a sender was not recognised, so a delivered message counted as undelivered."""
        for prompt, marker in (("[hub:m7] hello", "m7"), ("[hub:m262 from owner] task", "m262"),
                               ("[hub:m267 from hub] remember", "m267"), ("[hub:m9 from demo-2.x] ok", "m9"),
                               ("plain terminal input", None), ("[hub:x1] not a marker", None), ("[hub:m] not a marker", None),
                               # an id with a suffix is a marker too: the core sets mNaK, mNo, mNx5-<session>
                               ("[hub:m1a10 from core] input", "m1a10")):
            kind, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": prompt})
            self.assertEqual((kind, data["marker"]), ("turn.started", marker), prompt)

    def test_all_markers_at_line_starts(self):
        # Claude Code merged two stdin messages into one turn; the second marker is a delivery fact too
        prompt = "[hub:m5 from b] (arrived mid-turn) result\n[hub:m7 from hub] about the click message\nquote [hub:m9] in the text"
        kind, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": prompt})
        self.assertEqual((data["marker"], data["also"]), ("m5", ["m7"]))

    def test_task_notification_is_not_terminal_input(self):
        # a "Terminal input" feed entry with <task-notification> is Claude Code reporting that a
        # background task finished; the hook extracts its status and summary (the first 200 chars never reach them)
        prompt = ("<task-notification>\n<task-id>a6eddbf6d424b3f72</task-id>\n<tool-use-id>toolu_01RR</tool-use-id>\n"
                  "<output-file>/private/tmp/claude-501/" + "x" * 200 + "/tasks/a6.output</output-file>\n"
                  "<status>completed</status>\n<summary>Agent \"Code research\" completed</summary>\n</task-notification>")
        kind, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": prompt})
        self.assertEqual(data["notice"], {"status": "completed", "summary": 'Agent "Code research" completed',
                                          "ended": ["a6eddbf6d424b3f72"]})
        _, plain = translate({"hook_event_name": "UserPromptSubmit", "prompt": "plain input"})
        self.assertNotIn("notice", plain)


class PromptHeadMasked(unittest.TestCase):
    """The start of a turn went into the journal as written: a background `hub run` ended with a token in its command,
    and Claude Code's notification about it put the token into prompt_head whole. Known key forms are masked in the
    hook, over the whole prompt before the cut (a key across the 200th character keeps no tail); the label, the ended
    tasks and the paths stay. All values are made up."""
    KEY = "glpat-" + "Zq4" * 8

    def turn(self, prompt):
        kind, data = translate({"hook_event_name": "UserPromptSubmit", "prompt": prompt})
        self.assertEqual(kind, "turn.started")
        return data

    def test_a_key_in_a_task_notification_is_masked_and_the_rest_stays(self):
        prompt = (f"<task-notification>\n<task-id>b7x3k9a2c</task-id>\n<output-file>/home/x/maps/tasks/b7x3k9a2c.output"
                  f"</output-file>\n<status>completed</status>\n<summary>Background command \"hub run -- curl -H "
                  f"'PRIVATE-TOKEN: {self.KEY}' https://git.example.test/api\" completed (exit code 0)</summary>\n"
                  "</task-notification>")
        data = self.turn(prompt)
        self.assertNotIn(self.KEY[:12], json.dumps(data))
        self.assertIn("<task-id>b7x3k9a2c</task-id>\n<output-file>/home/x/maps/tasks/b7x3k9a2c.output", data["prompt_head"])
        self.assertEqual(data["notice"]["ended"], ["b7x3k9a2c"])
        self.assertEqual(data["notice"]["status"], "completed")
        self.assertIn(f"PRIVATE-TOKEN: {redact.MASK}' https://git.example.test/api", data["notice"]["summary"])

    def test_a_key_across_the_cut_leaves_no_tail(self):
        for at in range(180, 200):
            head = self.turn("[hub:m5 from maps] " + "x" * (at - 19) + " " + self.KEY + " done")["prompt_head"]
            self.assertNotIn("glpat-", head, at)
            self.assertTrue(head.startswith("[hub:m5 from maps] x"), at)
            self.assertLessEqual(len(head), 200)

    def test_the_label_and_plain_words_are_as_written(self):
        prompt = ("[hub:m12 from maps] Fix /home/x/maps/src/app.py:42 for ABC-123, commit 3f2a9c1e, session maps-1, "
                  "token: see the vault\n[hub:m14] and this")
        data = self.turn(prompt)
        self.assertEqual((data["marker"], data["also"], data["prompt_head"]), ("m12", ["m14"], prompt[:200]))
        data = self.turn(f"[hub:m12 from maps] use {self.KEY} here")
        self.assertEqual((data["marker"], data["prompt_head"]), ("m12", f"[hub:m12 from maps] use {redact.MASK} here"))


class CompactedDirection(unittest.TestCase):
    """A direction's session after context compaction did not run `hub direction resume`, though its skill says so,
    and the rules from the backlog documents fell out of its context. The core hands the backlog over itself: the
    SessionStart(compact) hook answers a direction's session with additionalContext; anyone else gets nothing new."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hook-compact-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state = os.path.join(self.tmp, "state")
        self.dirs = os.path.join(self.tmp, "directions")
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.state)
        subprocess.run(["git", "init", "-q", repo], check=True)
        subprocess.run(["git", "-C", repo, "-c", "user.name=t", "-c", "user.email=t@example.test", "commit", "-q",
                        "--allow-empty", "-m", "init"], check=True)   # a direction's repo has a commit
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith("HUB_")},
                    "HUB_STATE_DIR": self.state, "HUB_DIRECTIONS": self.dirs}
        new = subprocess.run([sys.executable, os.path.join(REPO, "modules", "directions", "direction.py"), "new", "demo",
                              "--repo", repo], env=self.env, capture_output=True, text=True)
        self.assertEqual(new.returncode, 0, new.stderr)
        self.doc(f"---\nid: doc-1\ntitle: Invariants\n---\n\n{RULE}\n")
        self.events = [("session.requested", "lead", {"cwd": repo, "session_id": "s-lead", **OWNER}),
                       ("session.requested", "plain", {"cwd": repo, "session_id": "s-plain", **OWNER}),
                       ("task.backlog", None, {"slug": "demo", "session": "lead", **OWNER})]
        self.journal()

    def journal(self, *more):
        self.events += more
        with open(os.path.join(self.state, "journal.jsonl"), "w", encoding="utf-8") as f:
            for seq, (type, entity, data) in enumerate(self.events, 1):
                f.write(json.dumps({"seq": seq, "ts": 1000 + seq, "type": type, "entity": entity, "data": data},
                                   ensure_ascii=False) + "\n")

    def doc(self, text, name="doc-1 - Invariants.md"):
        path = os.path.join(self.dirs, "demo", "backlog", "docs", name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def hook(self, session, source="compact"):
        """The hook as Claude Code runs it: (its stdout, the facts it put into the spool)."""
        payload = json.dumps({"hook_event_name": "SessionStart", "session_id": "s-" + session, "source": source})
        out = subprocess.run([sys.executable, HOOK], input=payload.encode(), capture_output=True, timeout=60,
                             env={**self.env, "HUB_SESSION": session})
        self.assertEqual(out.returncode, 0, out.stderr)
        spool = os.path.join(self.state, "spool")
        facts = []
        for name in sorted(os.listdir(spool)) if os.path.isdir(spool) else []:
            with open(os.path.join(spool, name), encoding="utf-8") as f:
                facts.append(json.load(f)["type"])
            os.unlink(os.path.join(spool, name))
        self.assertFalse(os.path.exists(os.path.join(self.state, "hook-errors.log")))
        return out.stdout.decode("utf-8"), facts

    def context(self, stdout):
        answer = json.loads(stdout)["hookSpecificOutput"]
        self.assertEqual(answer["hookEventName"], "SessionStart")
        return answer["additionalContext"]

    def test_a_lead_taken_off_the_direction_gets_nothing_after_compaction(self):
        # 2026-11-06: a session that had been a lead by mistake kept getting the direction's summary
        self.assertIn("demo", self.context(self.hook("lead")[0]))
        self.journal(("task.backlog_dropped", None, {"slug": "demo", "session": "lead", **OWNER}))
        stdout, facts = self.hook("lead")
        self.assertEqual((stdout.strip(), facts), ("", ["session.compacted"]))

    def test_a_direction_gets_its_backlog_after_compaction(self):
        stdout, facts = self.hook("lead")
        self.assertEqual(facts, ["session.compacted"])
        text = self.context(stdout)
        self.assertIn("hub direction resume demo", text)
        self.assertIn(RULE, text)

    def test_nothing_new_for_others_and_for_a_fresh_start(self):
        self.assertEqual(self.hook("plain"), ("", ["session.compacted"]))
        self.assertEqual(self.hook("lead", source="startup"), ("", ["session.started"]))
        self.assertEqual(self.hook("lead", source="resume"), ("", ["session.started"]))

    def test_a_failing_resume_gives_no_context_and_keeps_the_fact(self):
        shutil.rmtree(os.path.join(self.dirs, "demo"))   # the backlog is gone: resume exits with an error
        self.assertEqual(self.hook("lead"), ("", ["session.compacted"]))

    def test_a_long_backlog_fits_the_limit_and_names_the_documents(self):
        # Claude Code cuts additionalContext at 8000 characters and 200 lines: what is cut, the session reads itself
        big = self.doc("---\nid: doc-2\ntitle: Journal\n---\n\n" + "\n".join(f"entry {i}: " + "x" * 60 for i in range(400)),
                       "doc-2 - Journal.md")
        text = self.context(self.hook("lead")[0])
        self.assertLessEqual(len(text), 8000)
        self.assertLessEqual(len(text.split("\n")), 200)
        self.assertIn(RULE, text)
        self.assertIn(big, text)
        self.assertIn("hub direction resume demo", text)

    def test_a_slow_resume_gives_nothing(self):
        def slow(args, timeout, **kw):
            raise subprocess.TimeoutExpired(args, timeout)
        self.assertIsNone(hub_hook.compacted_context("lead", self.state, run=slow))
        # the hook's 10 s are spent (a slow state read): resume is not even started
        self.assertIsNone(hub_hook.compacted_context("lead", self.state, run=self.fail,
                                                     start=time.monotonic() - hub_hook.BUDGET))

    def test_a_renamed_direction_still_gets_it(self):
        # the session's process keeps its old name in HUB_SESSION until it is started again
        self.journal(("session.started", "lead", {}), ("session.renamed", "lead", {"to": "lead-2", **OWNER}))
        self.assertEqual(journal.load_state(self.state)["aliases"].get("was:lead"), "lead-2")   # renamed indeed
        self.assertIn(RULE, self.context(self.hook("lead")[0]))

    def test_one_huge_line_does_not_hide_what_follows(self):
        self.doc(f"---\nid: doc-1\ntitle: Invariants\n---\n\n{'x' * 9000}\n{RULE}\n")
        text = self.context(self.hook("lead")[0])
        self.assertIn(RULE, text)
        # the clipped line is a rule too: the session is told to read the whole thing and where
        self.assertIn("read the whole `hub direction resume demo`", text)
        self.assertIn(os.path.join(self.dirs, "demo", "backlog", "docs", "doc-1 - Invariants.md"), text)

    def test_documents_beyond_twenty_are_counted(self):
        for n in range(2, 26):
            self.doc(f"---\nid: doc-{n}\ntitle: Notes {n}\n---\n\n" + "note\n" * 20, f"doc-{n} - Notes {n}.md")
        head = self.context(self.hook("lead")[0]).partition("\n\n")[0]   # the header with the document paths
        self.assertIn("doc-20 - Notes 20.md", head)
        self.assertNotIn("doc-21 - Notes 21.md", head)
        self.assertIn("… and 5 more in ", head)


if __name__ == "__main__":
    unittest.main()
