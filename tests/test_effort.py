"""Reasoning level of sessions (owner decision 2026-10-05: make high the default and show it in the status bar).
Hub sessions load settings without the user source (setting_sources), so the owner's effortLevel never reaches them —
the core passes `--effort` itself: from hub.json (`effort`), high by default; an unknown value — no flag, a log entry.
The page gets the level the core started the session with (info.effort)."""
import tests  # noqa: F401 — keep the machine's settings out
import contextlib
import io
import json
import os
import tempfile
import unittest

from core import catalog
from tests.test_tools import HubCase


class EffortFlag(unittest.TestCase):
    def flags(self, **cfg):
        d = tempfile.mkdtemp(prefix="effort-")
        return catalog.launch_flags(tools=[], cfg={**catalog.load_config(d), **cfg}, catalog={}, kind="work", cwd=d,
                                    project="all", state_dir=d, name="s")

    def test_default_is_medium(self):
        # owner decision 2026-10-05: keep medium and add a command to set the effort
        flags = self.flags()
        self.assertEqual(flags[flags.index("--effort") + 1], "medium")

    def test_a_sessions_own_level_wins(self):
        d = tempfile.mkdtemp(prefix="effort-")
        flags = catalog.launch_flags(tools=[], cfg=catalog.load_config(d), catalog={}, kind="work", cwd=d,
                                     project="all", state_dir=d, name="s", effort="xhigh")
        self.assertEqual(flags[flags.index("--effort") + 1], "xhigh")

    def test_from_config(self):
        flags = self.flags(effort="max")
        self.assertEqual(flags[flags.index("--effort") + 1], "max")

    def test_unknown_level_is_not_passed_and_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            flags = self.flags(effort="extreme")
        self.assertNotIn("--effort", flags)
        self.assertIn("extreme", err.getvalue())


class EffortCommand(unittest.TestCase):
    """So that a higher effort can be requested when medium is not enough: the owner, the hub or the session itself
    sets the session's level; an idle session is restarted with its earlier conversation and this level."""

    def test_set_level_then_respawn_with_it(self):
        from core import reducer
        st, seq = reducer.empty(), [0]
        def ev(t, e=None, **d):
            nonlocal st
            seq[0] += 1
            st, acts, why = reducer.reduce(st, {"seq": seq[0], "ts": 100 + seq[0], "type": t, "entity": e, "data": d})
            return acts, why
        owner = {"channel": "cli", "owner": True}
        ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **owner)
        ev("session.requested", "maps", cwd="/m", session_id="s-m", **owner)
        ev("session.requested", "seo", cwd="/s", session_id="s-s", **owner)
        ev("session.started", "maps")
        _, why = ev("session.effort_set", "maps", level="high", channel="session", owner=False, sender="seo")
        self.assertIsNotNone(why)                                                  # someone else's — no
        _, why = ev("session.effort_set", "maps", level="extreme", **owner)
        self.assertIn("medium", why or "")                                         # unknown level
        acts, why = ev("session.effort_set", "maps", level="high", channel="session", owner=False, sender="maps")
        self.assertIsNone(why)                                                     # to itself — allowed
        spawn = [a for a in acts if a.get("do") == "spawn"]
        self.assertEqual([a.get("effort") for a in spawn], ["high"])               # idle — restarted right away, with high
        self.assertEqual(st["sessions"]["maps"]["effort"], "high")
        ev("session.started", "maps")
        acts, why = ev("session.effort_set", "maps", level="default", **owner)
        self.assertIsNone(why)                                                     # back to the default (the page)
        self.assertNotIn("effort", st["sessions"]["maps"])
        self.assertEqual([a.get("effort") for a in acts if a.get("do") == "spawn"], [None])


class EffortInSnapshot(HubCase):
    def setUp(self):
        super().setUp()
        self.hub.host.kill_server()
        cfg = json.load(open(os.environ["HUB_CONFIG"]))
        with open(os.environ["HUB_CONFIG"], "w") as f:
            json.dump({**cfg, "effort": "xhigh"}, f)
        self.hub = self.new_hub()
        self.hub.start()

    def test_launch_line_and_session_info_carry_the_level(self):
        self.put("session.requested", "a", cwd=self.project, session_id="sid-a")
        self.until(lambda: (self.sess("a").get("info") or {}).get("effort"))
        argv = self.argvs("a")[0]
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")
        self.assertEqual(self.sess("a")["info"]["effort"], "xhigh")


if __name__ == "__main__":
    unittest.main()
