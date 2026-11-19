"""A model and effort chosen by a person by hand are not changed by the core (owner decision 2026-10-13: manual /model
settings by users must be respected — if the user chose another model, do not change it).
`/model sonnet` in a screenless session applies «for this session only» and is lost on a --resume start (checked on
Claude Code 2.1.296): every hub restart, parking or reload would bring back the default model. The owner's typed
`/model X` and `/effort X` the core remembers for the session, like `hub session model|effort`, and raises it with them."""
import tests  # noqa: F401 — do not see the machine's settings
import os
import unittest
import unittest.mock

from core import modelrule, reducer

OWNER = {"channel": "web", "owner": True}


class ManualModel(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-hub", kind="hub", **OWNER)
        self.ev("session.requested", "maps", cwd="/m", session_id="s-m", **OWNER)
        for n in ("hub", "maps"):
            self.ev("session.started", n)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def say(self, text, **who):
        self.assertIsNone(self.ev("message.received", None, to="maps", text=text, **(who or OWNER)))

    def respawn(self):
        """What the core will raise the session with again (after a hub restart, parking, reload — with --resume)."""
        return reducer.spawn_action(self.st["sessions"]["maps"], st=self.st)

    def test_the_owners_model_survives_a_restart(self):
        self.say("/model sonnet")
        self.say("/effort high")
        sess = self.st["sessions"]["maps"]
        self.assertEqual((sess.get("model"), sess.get("effort")), ("sonnet", "high"))
        self.assertFalse(sess.get("dirty"))           # Claude Code has already switched by itself — do not re-raise for this
        spawn = self.respawn()
        self.assertEqual((spawn.get("model"), spawn.get("effort")), ("sonnet", "high"))

    def test_default_drops_it_and_junk_changes_nothing(self):
        self.say("/model sonnet")
        self.say("/model default")
        self.assertNotIn("model", self.st["sessions"]["maps"])
        for text in ("/model", "/model gpt-9", "/effort huge", "/modelling x"):
            self.say(text)
        self.assertNotIn("model", self.st["sessions"]["maps"])
        self.assertNotIn("effort", self.st["sessions"]["maps"])

    def test_a_model_the_core_does_not_know_drops_the_old_choice(self):
        # review: Claude Code accepts `/model opusplan` or `sonnet[1m]`, the core does not; the earlier «sonnet» would
        # come back on a start on top of its latest choice
        self.say("/model sonnet")
        self.say("/model opusplan")
        self.assertNotIn("model", self.st["sessions"]["maps"])
        self.say("/model claude-opus-4-5")                     # the core knows the full id
        self.assertEqual(self.st["sessions"]["maps"].get("model"), "claude-opus-4-5")
        self.say("/effort high")
        self.say("/effort hihg")                               # Claude Code will not accept a typo — the choice stays
        self.assertEqual(self.st["sessions"]["maps"].get("effort"), "high")

    def test_only_a_person_sets_it(self):
        # «chosen by a person»: another session's command arrives as text, the hub sets the model only with its own command
        self.say("/model haiku", channel="session", owner=False, sender="hub")
        self.assertNotIn("model", self.st["sessions"]["maps"])


class OwnModelEnv(unittest.TestCase):
    def test_an_own_model_in_the_environment_gets_no_pair_by_default(self):
        with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_MODEL": "my-model"}):
            os.environ.pop("ANTHROPIC_BASE_URL", None)
            self.assertEqual((modelrule.pin("normal", {}), modelrule.pin("risky", {})), ({}, {}))


if __name__ == "__main__":
    unittest.main()
