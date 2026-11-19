"""Session model (owner request 2026-10-05: change the model for anyone). Like the reasoning level: the owner, the hub or
the session itself sets the session's model (`hub session model`, the picker under the input field); an idle session
is brought up again with the same conversation and `claude --model`; `default` — back to the default model."""
import tests  # noqa: F401 — don't see the machine's settings
import contextlib
import io
import tempfile
import unittest

from core import catalog, reducer


class ModelFlag(unittest.TestCase):
    def flags(self, model=None, cfg=None):
        d = tempfile.mkdtemp(prefix="model-")
        return catalog.launch_flags(tools=[], cfg={**catalog.load_config(d), **(cfg or {})}, catalog={}, kind="work", cwd=d,
                                    project="all", state_dir=d, name="s", model=model)

    def test_no_model_no_flag(self):
        self.assertNotIn("--model", self.flags())        # the default model is Claude Code's own

    def test_short_name_as_is(self):
        flags = self.flags(model="sonnet")              # Claude Code picks the family's latest model itself
        self.assertEqual(flags[flags.index("--model") + 1], "sonnet")

    def test_full_id_and_config(self):
        flags = self.flags(model="claude-opus-5-5")
        self.assertEqual(flags[flags.index("--model") + 1], "claude-opus-5-5")
        flags = self.flags(cfg={"model": "haiku"})     # for everyone — from hub.json
        self.assertEqual(flags[flags.index("--model") + 1], "haiku")
        flags = self.flags(model="fable", cfg={"model": "haiku"})
        self.assertEqual(flags[flags.index("--model") + 1], "fable")   # the session's own model wins

    def test_unknown_model_no_flag_and_logged(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertNotIn("--model", self.flags(model="gpt-9"))
        self.assertIn("gpt-9", err.getvalue())


class ModelCommand(unittest.TestCase):
    def test_set_model_then_respawn_with_it(self):
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
        _, why = ev("session.model_set", "maps", model="opus", channel="session", owner=False, sender="seo")
        self.assertIsNotNone(why)                                                  # someone else's — no
        _, why = ev("session.model_set", "maps", model="gpt-9", **owner)
        self.assertIn("sonnet", why or "")                                         # unknown model
        acts, why = ev("session.model_set", "maps", model="opus", channel="session", owner=False, sender="hub")
        self.assertIsNone(why)                                                     # the hub — allowed
        spawn = [a for a in acts if a.get("do") == "spawn"]
        self.assertEqual([a.get("model") for a in spawn], ["opus"])               # idle — respawned at once
        ev("session.started", "maps")
        acts, why = ev("session.model_set", "maps", model="default", channel="session", owner=False, sender="maps")
        self.assertIsNone(why)                                                     # for itself, back to the default
        self.assertNotIn("model", st["sessions"]["maps"])
        self.assertEqual([a.get("model") for a in acts if a.get("do") == "spawn"], [None])


if __name__ == "__main__":
    unittest.main()
