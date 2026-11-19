"""The sessions slice of a module (Module API 2.10) carries where an executor came from: raised_by (the session that raised
it), since (when the session's current state began; for one that handed in - when it handed in) and worktree
{branch, base} of its working copy. No path of the copy or the repository: the sessions right promises no texts and no
paths."""
import tests  # noqa: F401 - ignore the machine's settings
import json
import unittest

from core import executors, modstate, reducer

OWNER = {"channel": "cli", "owner": True}
COPY = {"path": "/home/x/.claude/worktrees/fix", "branch": "task/fix", "base": "main", "repo": "/home/x/repo",
        "keep_branch": False, "from_main": True, "base_sha": "0123abc"}


class Origin(unittest.TestCase):
    def setUp(self):
        self.st, self.seq = reducer.empty(), 0
        self.ev("session.requested", "hub", cwd="/home/x/hub", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.started", "hub")
        self.ev("session.requested", "docs", cwd="/home/x/docs", session_id="s-docs", **OWNER)
        self.ev("session.started", "docs")
        self.ev("turn.started", "docs", marker="m1")
        self.ev("task.run_requested", "fix", task="fix the thing", channel="session", owner=False, sender="docs")
        for kind, entity, data in executors.events_for(self.st, "fix", self.st["runs"]["fix"], cwd=COPY["path"],
                                                       worktree=COPY):
            self.ev(kind, entity, **data)
        self.ev("session.started", "fix")

    def ev(self, kind, entity=None, /, **data):
        self.seq += 1
        self.st, _, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": kind,
                                                   "entity": entity, "data": data})
        self.assertIsNone(why, (kind, data))

    def rows(self):
        return modstate.session_rows(self.st)["sessions"]

    def test_an_executor_row_says_who_raised_it_and_on_which_branch(self):
        row = self.rows()["fix"]
        self.assertEqual((row["raised_by"], row["worktree"]), ("docs", {"branch": "task/fix", "base": "main"}))

    def test_a_renamed_raiser_is_followed(self):
        self.ev("turn.result", "docs", text="assigned fix")
        self.ev("turn.stopped", "docs")   # a busy session is renamed once it is free
        self.ev("session.renamed", "docs", to="docs2", **OWNER)
        self.assertIn("docs2", self.st["sessions"])
        self.assertEqual(self.rows()["fix"]["raised_by"], "docs2")

    def test_a_session_nobody_raised_has_nulls(self):
        for n in ("hub", "docs"):
            row = self.rows()[n]
            self.assertEqual((row["raised_by"], row["worktree"]), (None, None), n)

    def test_since_is_when_the_current_state_began(self):
        for n, row in self.rows().items():
            self.assertEqual(row["since"], self.st["sessions"][n]["since"], n)
        self.st["sessions"]["fix"].update(state="retired", since=5000.0)   # handed in at that time
        row = self.rows()["fix"]
        self.assertEqual((row["retired"], row["since"]), (True, 5000.0))

    def test_a_row_of_an_older_state_has_nulls_not_a_crash(self):
        self.st["sessions"]["fix"].pop("since")
        self.st["sessions"]["fix"]["worktree"] = {"path": "/home/x/w"}   # no branch known
        self.st["sessions"]["docs"]["raised_by"] = 7                      # not a name
        rows = self.rows()
        self.assertEqual((rows["fix"]["since"], rows["fix"]["worktree"]), (None, {"branch": None, "base": None}))
        self.assertIsNone(rows["docs"]["raised_by"])

    def test_no_path_of_the_copy_or_the_repository_is_in_the_slice(self):
        text = json.dumps(modstate.session_rows(self.st), ensure_ascii=False)
        for secret in (COPY["path"], COPY["repo"], "/home/x", "0123abc"):
            self.assertNotIn(secret, text)
        self.assertEqual(set(self.rows()["fix"]["worktree"]), {"branch", "base"})
        # a branch name has a slash, a path starts with one: no value in the new fields is an absolute path
        for row in self.rows().values():
            for v in (row["raised_by"], *(row["worktree"] or {}).values()):
                self.assertFalse(str(v).startswith("/"), v)


if __name__ == "__main__":
    unittest.main()
