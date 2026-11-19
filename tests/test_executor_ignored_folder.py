"""A specialist that works in a folder git does not have (ignored, so its working copy lacks it), end to end: a real
hubd, real git, session processes (a fake claude). Its executor starts in the root of its copy and its first message names
the source folder by its absolute path — for a task it raises and for a message the core hands over while it is busy."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import subprocess
import unittest

from tests import test_stream_hubd

HUB = {"channel": "session", "owner": False, "sender": "hub"}
OPS = {"channel": "session", "owner": False, "sender": "ops"}


class IgnoredFolderLive(test_stream_hubd.StreamHubdTest):
    # only its own scenarios: the StreamHubdTest tests already run in their own module
    locals().update({n: None for n in dir(test_stream_hubd.StreamHubdTest) if n.startswith("test_")})

    def specialist(self, name):
        top = os.path.realpath(self.repo())
        with open(os.path.join(top, ".gitignore"), "w") as f:
            f.write("out/\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x"}
        for args in (("add", ".gitignore"), ("commit", "-q", "-m", "ignore out")):
            subprocess.run(["git", "-C", top, *args], check=True, capture_output=True, env=env)
        out = os.path.join(top, "out")
        os.makedirs(out)
        with open(os.path.join(out, "report.md"), "w") as f:
            f.write("week 1\n")
        self.new_session(name, role="agent", about="reports", cwd=out)
        return top, out

    def started_in_the_root(self, name, top, out):
        self.assertEqual([e["data"] for e in self.events("task.run_failed")], [])   # before: "the copy has no folder out"
        x = self.sess(name)
        root = os.path.join(top, ".claude", "worktrees", name)
        self.assertEqual((x["role"], x["cwd"], x["worktree"]["path"]), ("executor", root, root))
        self.assertEqual(x["worktree"]["source_cwd"], out)
        self.assertFalse(os.path.exists(os.path.join(root, "out")))   # the copy does not have the folder

    def test_a_task_of_a_specialist_in_an_ignored_folder_starts_in_the_root_with_the_folder_in_the_brief(self):
        top, out = self.specialist("rep")
        self.cli("task.run_requested", "rep-week", task="add week 2 to report.md", channel="session", owner=False,
                 sender="rep")
        self.until(lambda: self.sess("rep-week").get("state") == "idle"
                   or self.events("task.run_failed"), timeout=15)
        self.started_in_the_root("rep-week", top, out)
        (d,) = [d for d in self.hub.state["delegations"].values() if d["session"] == "rep-week"]
        self.assertIn(f"The raiser's folder {out} is not in your working copy", d["context"])

    def test_a_message_handed_over_from_a_busy_specialist_names_the_folder_too(self):
        # no brief of the core here: the executor gets the specialist's own message with the core's head before it
        self.new_session("hub", kind="hub")
        self.new_session("ops", role="agent", about="operations")
        top, out = self.specialist("rep")
        self.send("rep", "/slow")
        self.until(lambda: self.sess("rep")["state"] == "busy")
        self.cli("delegation.created", None, session="rep", context="add week 2 to report.md", for_session="ops", **OPS)
        self.until(lambda: any(d["session"] == "rep" for d in self.hub.state["delegations"].values()))
        d = next(d for d in self.hub.state["delegations"].values() if d["session"] == "rep")
        self.cli("message.routed", d["id"], route="executor", **HUB)
        self.until(lambda: any(s.get("role") == "executor" and s.get("state") == "idle"
                               for s in self.hub.state["sessions"].values()) or self.events("task.run_failed"),
                   timeout=15)
        (name,) = [n for n, s in self.hub.state["sessions"].items() if s.get("role") == "executor"]
        self.started_in_the_root(name, top, out)
        text = self.hub.state["messages"][d["message"]]["text"]
        self.assertIn(f"The raiser's folder {out} is not in your working copy", text)
        self.assertGreater(text.index("is not in your working copy"), text.index("add week 2 to report.md"))   # after its words
        # the specialist is told what was taken in its words, not in the core's note
        self.assertTrue(any(m["session"] == "rep" and "(\"Task from session ops.\nadd week 2" in m["text"]
                            for m in self.hub.state["messages"].values()))


if __name__ == "__main__":
    unittest.main()
