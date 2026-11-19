"""The version of a set in the catalog is `git rev-parse` with a timeout (review of PR #18): re-reading the catalog is a
core tick step, and a hung git (network disk, index lock) would hold the core, including on shutdown. If it does not
answer in time, there is no version."""
import tests  # noqa: F401 — keep the machine's settings out
import subprocess
import tempfile
import unittest
from unittest import mock

from core import catalog


def hanging_git(args, **kw):
    if "timeout" not in kw:
        raise AssertionError("git without a timeout would hang forever")
    raise subprocess.TimeoutExpired(args, kw["timeout"])


class GitVersion(unittest.TestCase):
    def test_hanging_git_gives_no_version(self):
        # on Windows git calls platforms.run via Popen (kills the tree) — patching subprocess.run does not reach it
        with mock.patch.object(catalog.subprocess, "run", hanging_git), \
                mock.patch.object(catalog.platforms.current, "run",
                                  lambda args, timeout, **kw: hanging_git(args, timeout=timeout, **kw)):
            self.assertIsNone(catalog._git_version("."))

    def test_a_set_without_a_marketplace_runs_no_git(self):
        # a set without .claude-plugin/marketplace.json (path moved, directory deleted) is skipped as before: git takes
        # up to 10 s in a core tick step, and such a set has no tools
        calls = []
        with mock.patch.object(catalog, "_git_version", side_effect=lambda path: calls.append(path)):
            catalog.build({"sets": [{"name": "gone", "path": tempfile.mkdtemp()}]})
        self.assertEqual([c for c in calls if c != catalog.REPO], [])


if __name__ == "__main__":
    unittest.main()
