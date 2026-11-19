"""Complaint 2026-10-03: the new version is not shown, yet everyone has an update button, even those who need no update.
The hub compares the installed plugin's commit with the author's latest; if there is a newer one, the version and
"Check and update" are shown, if nothing to update, no button. Updating a checked one is exactly the checked commit."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os

from modules.market import market
from tests import test_market


class Updates(test_market.Market):
    def test_newer_commit_is_found_and_reviewed_update_installs_it(self):
        m = self.m()
        self.put(m, "alice/hub-mascot")
        self.assertEqual(m.updates(), {})                               # nothing to update
        src = os.path.join(self.gh, "alice", "hub-mascot")
        with open(os.path.join(src, market.MANIFEST)) as f:
            man = json.load(f)
        man["version"] = "2.0.0"
        with open(os.path.join(src, market.MANIFEST), "w") as f:
            json.dump(man, f)
        test_market.git(src, "commit", "-qam", "2.0.0")
        up = m.updates()
        self.assertEqual(list(up), ["alice:mascot"])
        self.assertEqual(up["alice:mascot"]["version"], "2.0.0")
        head = up["alice:mascot"]["commit"]
        m.stage("alice/hub-mascot")
        m.verdict("alice/hub-mascot", "ok", "the same owl")
        m.update("alice:mascot", reviewed=True)
        self.assertEqual(self.cfg()["extensions"]["alice:mascot"]["commit"], head)
        self.assertEqual(m.updates(), {})

    def test_clone_already_newer_than_record_update_records_it(self):
        """2026-10-09: the clone on disk is already at the author's last commit but the settings record an old one:
        "newer exists" is lit, while the update answered "already the latest version" and changed nothing: the button
        never went away."""
        m = self.m()
        self.put(m, "alice/hub-mascot")
        src = os.path.join(self.gh, "alice", "hub-mascot")
        test_market.git(src, "commit", "-q", "--allow-empty", "-m", "next")
        clone = os.path.join(m.store, "alice", "hub-mascot")
        test_market.git(clone, "fetch", "-q", "--depth", "1", "origin")
        test_market.git(clone, "reset", "-q", "--hard", "FETCH_HEAD")
        self.assertEqual(list(m.updates()), ["alice:mascot"])
        self.again(m, "alice:mascot", "alice/hub-mascot")
        self.assertEqual(m.updates(), {})


def load_tests(loader, standard, pattern):
    import unittest
    return unittest.TestSuite(Updates(n) for n in Updates.__dict__ if n.startswith("test"))
