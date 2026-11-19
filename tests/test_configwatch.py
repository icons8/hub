"""Settings that change how the core behaves, watched for edits outside the page (core/configwatch.py, dev-guard spec
§5.2, slice 1b). A colleague's hub moved every session and every service call of the core to the most expensive model
by a line in the owner's settings.json; nothing showed it. Now hubd keeps the last accepted state of these keys: a page
edit is accepted at once, any other edit gives config.changed_outside {key, old, new} once and a row in the snapshot; a
secret is kept only as a fingerprint; the first check after an update accepts everything as it is."""
import tests  # noqa: F401 — do not see this machine's settings
import json
import os
import tempfile
import types
import unittest
from unittest import mock

import platforms
from core import catalog, configwatch, journal, spool
from core.daemon import Hub
from core import reducer
from core.views import Reader

OWNER = {"channel": "web", "owner": True}


class Watch(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="cfgwatch-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.claude = os.path.join(self.state, "claude")
        os.makedirs(self.claude)
        self.cfg = os.path.join(self.state, "hub.json")
        self.now = [1000.0]
        env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg, "CLAUDE_CONFIG_DIR": self.claude})
        env.start()
        self.addCleanup(env.stop)
        self.hub_json({"model": "sonnet", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settings({"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-x", "CLAUDE_CODE_OAUTH_TOKEN": "tok-first-1"})
        self.hub = Hub(self.state, types.SimpleNamespace(poll=lambda: []), clock=lambda: self.now[0])
        self.hub.execute = lambda a: None

    def hub_json(self, cfg):
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.bump(self.cfg)

    def settings(self, env):
        path = os.path.join(self.claude, "settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"env": env}, f)
        self.bump(path)

    def bump(self, path):
        """A new mtime even within the same clock tick of the file system."""
        self.mtime = getattr(self, "mtime", 1_700_000_000) + 7
        os.utime(path, (self.mtime, self.mtime))

    def check(self, after=0):
        """One tick's steps that matter here: hub.json re-read when it changed, then the watch."""
        self.now[0] += after
        self.hub.reload_catalog_if_due()
        self.hub.watch_config()

    def settle(self):
        self.check()
        self.check(configwatch.SETTLE + 1)

    def rows(self):
        """The rows "changed outside the page" the fold keeps, the newest first (the page does not show them)."""
        rows = (self.hub.state.get("config_seen") or {}).get("rows") or {}
        return [{"key": k, **r} for k, r in sorted(rows.items(), key=lambda kv: -kv[1]["ts"])]

    def snap(self):
        r = Reader()
        r.state = reducer.copy_state(self.hub.state)
        return r.snapshot(self.now[0])

    def facts(self, kind="config.changed_outside"):
        return [e["data"] for e in journal.read(self.hub.journal.path) if e["type"] == kind]

    def test_the_first_check_accepts_everything_as_it_is(self):
        self.settle()
        self.assertEqual(self.facts(), [])
        seen = self.facts("config.seen")
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0]["first"])
        self.assertEqual(seen[0]["values"]["model"], "sonnet")
        self.assertEqual(seen[0]["values"]["settings.env.ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-x")
        self.assertEqual(self.rows(), [])

    def test_a_file_edit_gives_one_fact_with_old_and_new(self):
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        self.assertEqual(self.facts(), [{"key": "model", "old": "sonnet", "new": "opus"}])
        rows = self.rows()
        self.assertEqual([(r["key"], r["old"], r["new"]) for r in rows], [("model", "sonnet", "opus")])

    def test_a_model_name_in_settings_json_shows_its_value(self):
        self.settle()
        self.settings({"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-opus-y", "CLAUDE_CODE_OAUTH_TOKEN": "tok-first-1"})
        self.settle()
        self.assertEqual(self.facts(), [{"key": "settings.env.ANTHROPIC_DEFAULT_SONNET_MODEL",
                                         "old": "claude-sonnet-x", "new": "claude-opus-y"}])

    def test_the_same_edit_again_is_one_fact(self):
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        for _ in range(3):   # the file written again with the same content, ticks and the periodic check go on
            self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
            self.settle()
            self.check(configwatch.EVERY + 1)
        self.assertEqual(len(self.facts()), 1)

    def test_a_page_edit_gives_no_fact(self):
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})   # the page writes the file
        self.hub.record("settings.changed", None, {"what": "model", "old": "sonnet", "new": "opus", **OWNER})
        self.settle()
        self.check(configwatch.EVERY + 1)
        self.assertEqual(self.facts(), [])
        self.assertEqual(self.hub.state["config_seen"]["values"]["model"], "opus")
        self.assertEqual(self.rows(), [])

    def test_a_page_edit_seen_before_its_event_gives_no_fact(self):
        # the file is written first, its event comes a moment later: a check in between must not call it outside
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.check()
        self.check(1)   # hubd ticks several times a second: the event may still be on its way
        self.hub.record("settings.changed", None, {"what": "model", "old": "sonnet", "new": "opus", **OWNER})
        self.check(1)
        self.check(configwatch.SETTLE + 1)
        self.assertEqual(self.facts(), [])

    def test_a_page_edit_of_a_pair_key_covers_its_leaf(self):
        self.hub_json({"executors": {"parallel": 3}})
        self.settle()
        self.hub_json({"executors": {"parallel": 5}})
        self.hub.record("settings.changed", None, {"what": "executors.parallel", "old": 3, "new": 5, **OWNER})
        self.settle()
        self.assertEqual(self.facts(), [])

    def test_a_page_mark_does_not_cover_a_later_edit_by_hand(self):
        self.settle()
        self.hub.record("settings.changed", None, {"what": "model", "old": "sonnet", "new": "sonnet", **OWNER})
        self.settle()   # the page saved the same value: nothing changed, the mark is spent
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        self.assertEqual(self.facts(), [{"key": "model", "old": "sonnet", "new": "opus"}])

    def test_a_token_is_kept_as_a_fingerprint_without_its_value(self):
        self.settle()
        self.settings({"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-x", "CLAUDE_CODE_OAUTH_TOKEN": "tok-second-2"})
        self.settle()
        facts = self.facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["key"], "settings.env.CLAUDE_CODE_OAUTH_TOKEN")
        self.assertTrue(facts[0]["hidden"])
        self.assertNotEqual(facts[0]["old"], facts[0]["new"])
        with open(self.hub.journal.path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("tok-first-1", text)
        self.assertNotIn("tok-second-2", text)
        row = self.rows()[0]
        self.assertTrue(row["hidden"])
        self.assertNotIn("tok-", json.dumps(row))
        self.assertNotIn("config_outside", self.snap())

    def test_any_env_value_but_a_model_name_is_a_fingerprint(self):
        # an address may carry a password or a team's host: only "changed"
        self.settings({"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-x", "CLAUDE_CODE_OAUTH_TOKEN": "tok-first-1",
                       "ANTHROPIC_BASE_URL": "https://203.0.113.5/one"})
        self.settle()
        self.settings({"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-x", "CLAUDE_CODE_OAUTH_TOKEN": "tok-first-1",
                       "ANTHROPIC_BASE_URL": "https://203.0.113.5/two"})
        self.settle()
        self.assertEqual([(f["key"], f.get("hidden")) for f in self.facts()], [("settings.env.ANTHROPIC_BASE_URL", True)])
        with open(self.hub.journal.path, encoding="utf-8") as f:
            self.assertNotIn("203.0.113.5", f.read())

    def test_a_secret_looking_key_of_hub_json_is_a_fingerprint_too(self):
        self.hub_json({"tailnet": {"auth_key": "tskey-one"}})
        self.settle()
        self.hub_json({"tailnet": {"auth_key": "tskey-two"}})
        self.settle()
        with open(self.hub.journal.path, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("tskey-", text)
        self.assertEqual([f["key"] for f in self.facts()], ["tailnet.auth_key"])

    def test_a_default_of_the_core_is_not_an_edit(self):
        # a release that adds a default of a watched key must not show up as a change on every hub
        self.settle()
        with mock.patch.dict(catalog.DEFAULTS, {"effort": "high", "claude_config_dir": "/home/x/claude"}):
            self.hub_json({"model": "sonnet", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"], "title": "maps"})
            self.settle()
        self.assertEqual(self.facts(), [])
        self.assertNotIn("effort", self.hub.state["config_seen"]["values"])

    def test_a_name_not_passed_to_sessions_is_not_watched(self):
        self.settle()
        self.settings({"ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-x", "CLAUDE_CODE_OAUTH_TOKEN": "tok-first-1",
                       "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-fable-z"})   # not in user_env: the terminal's only
        self.settle()
        self.assertEqual(self.facts(), [])

    def test_a_second_edit_keeps_the_first_old_and_going_back_clears_the_row(self):
        self.settle()
        for model in ("opus", "haiku"):
            self.hub_json({"model": model, "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
            self.settle()
        rows = self.rows()
        self.assertEqual([(r["old"], r["new"]) for r in rows], [("sonnet", "haiku")])
        self.hub_json({"model": "sonnet", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        self.assertEqual(len(self.facts()), 3)
        self.assertEqual(self.rows(), [])

    def test_a_page_edit_after_an_outside_one_clears_its_row(self):
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        self.hub_json({"model": "haiku", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.hub.record("settings.changed", None, {"what": "model", "old": "opus", "new": "haiku", **OWNER})
        self.settle()
        self.assertEqual(len(self.facts()), 1)
        self.assertEqual(self.rows(), [])

    def test_an_unreadable_settings_json_is_not_a_removal(self):
        self.settle()
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            f.write("{ not json")
        self.bump(os.path.join(self.claude, "settings.json"))
        self.settle()
        self.assertEqual(self.facts(), [])

    def test_a_page_event_from_anyone_but_the_page_marks_nothing(self):
        # review: a session that edits a key and names it "saved from the page" must still show up
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.hub.record("settings.changed", None, {"what": "model", "old": "sonnet", "new": "opus",
                                                   "channel": "session", "sender": "maps"})
        self.settle()
        self.assertEqual(self.facts(), [{"key": "model", "old": "sonnet", "new": "opus"}])

    def test_a_page_event_through_the_mailbox_from_a_session_marks_nothing(self):
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        spool.put(os.path.join(self.state, "spool"), "settings.changed", None,
                  {"what": "model", "old": "sonnet", "new": "opus", "channel": "session", "sender": "maps"}, src="cli")
        self.hub.ingest()
        self.settle()
        self.assertEqual(self.facts(), [{"key": "model", "old": "sonnet", "new": "opus"}])

    def test_setting_sources_from_the_page_brings_the_settings_values_silently(self):
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump({"model": "opus", "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-fable-z"}}, f)
        self.bump(os.path.join(self.claude, "settings.json"))
        self.settle()
        for sources in ("user,project,local", "project,local"):
            self.hub_json({"model": "sonnet", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"], "setting_sources": sources})
            self.hub.record("settings.changed", None, {"what": "setting_sources", "new": sources, **OWNER})
            self.settle()
        self.assertEqual(self.facts(), [])

    def test_a_broken_settings_json_does_not_keep_what_is_no_longer_passed(self):
        self.hub_json({"model": "sonnet", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"], "setting_sources": "user,project,local"})
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump({"model": "opus"}, f)
        self.bump(os.path.join(self.claude, "settings.json"))
        self.settle()
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            f.write("{ not json")
        self.bump(os.path.join(self.claude, "settings.json"))
        self.hub_json({"model": "sonnet", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"], "setting_sources": "project,local"})
        self.hub.record("settings.changed", None, {"what": "setting_sources", "new": "project,local", **OWNER})
        self.settle()
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump({"model": "opus"}, f)
        self.bump(os.path.join(self.claude, "settings.json"))
        self.settle()
        self.assertEqual(self.facts(), [])

    def test_an_edit_after_a_row_went_from_the_page_starts_from_the_lived_value(self):
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        self.now[0] += configwatch.ROW_DAYS * 86400 + 60
        self.hub_json({"model": "haiku", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        rows = self.rows()
        self.assertEqual([(r["old"], r["new"]) for r in rows], [("opus", "haiku")])

    def test_a_broken_settings_json_still_compares_hub_json(self):
        self.settle()
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            f.write("{ not json")
        self.bump(os.path.join(self.claude, "settings.json"))
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        self.assertEqual(self.facts(), [{"key": "model", "old": "sonnet", "new": "opus"}])

    def test_the_page_saving_the_new_value_clears_the_row(self):
        # the owner agrees with an outside edit and saves the same value on the page: nothing differs, the row goes
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.hub.record("settings.changed", None, {"what": "model", "old": "opus", "new": "opus", **OWNER})
        self.settle()
        self.assertEqual(self.rows(), [])

    def test_the_snapshot_of_the_page_carries_no_rows(self):
        # the owner put the block on hold (06.11): the core watches quietly, nothing about it goes to the page
        self.settle()
        self.hub_json({"model": "opus", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"]})
        self.settle()
        self.assertEqual([r["key"] for r in self.rows()], ["model"])
        self.assertNotIn("config_outside", self.snap())

    def test_a_secret_under_a_secret_named_section_is_hidden(self):
        self.hub_json({"access": {"tokens": {"phone": "abc-one"}}})
        self.settle()
        self.hub_json({"access": {"tokens": {"phone": "abc-two"}}})
        self.settle()
        with open(self.hub.journal.path, encoding="utf-8") as f:
            self.assertNotIn("abc-", f.read())
        self.assertEqual([(f["key"], f.get("hidden")) for f in self.facts()], [("access.tokens.phone", True)])

    def test_a_long_value_made_short_stays_a_hidden_row(self):
        # review: the row keeps the first old value — a fingerprint — so it stays hidden whatever comes next
        self.hub_json({"user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"], "model": "x" * (configwatch.SHOWN_MAX + 10)})
        self.settle()
        for model in ("a", "b"):
            self.hub_json({"user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"], "model": model})
            self.settle()
        row = self.rows()[0]
        self.assertEqual((row["key"], row.get("hidden"), configwatch.hidden(row["old"])), ("model", True, True))

    def test_with_user_sources_the_settings_model_names_are_watched(self):
        # sessions that read settings.json whole get its model and every model name of its env
        self.hub_json({"model": "sonnet", "user_env": ["ANTHROPIC_DEFAULT_SONNET_MODEL"], "setting_sources": "user,project,local"})
        self.settle()
        with open(os.path.join(self.claude, "settings.json"), "w") as f:
            json.dump({"model": "opus", "env": {"ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-fable-z"}}, f)
        self.bump(os.path.join(self.claude, "settings.json"))
        self.settle()
        got = {f["key"]: f["new"] for f in self.facts()}
        self.assertEqual(got.get("settings.model"), "opus")
        self.assertEqual(got.get("settings.env.ANTHROPIC_DEFAULT_OPUS_MODEL"), "claude-fable-z")

    def test_odd_values_of_the_watched_keys_do_not_crash_the_tick(self):
        self.settle()
        for odd in ("abc", "10", -1, 0.5, None, [1, {"a": None}], {"x": {"y": float("inf")}}, True):
            self.hub_json({key: odd for key in ("model", "effort", "executors", "critic", "session_env_values",
                                                 "setting_sources", "claude_config_dir", "tailnet", "access",
                                                 "user_env")})
            self.settle()
        self.assertTrue(self.facts())

    def test_the_facts_are_not_taken_from_the_mailbox(self):
        self.settle()
        box = os.path.join(self.state, "spool")
        for kind, data in (("config.seen", {"values": {"model": "opus"}, **OWNER}),
                           ("config.changed_outside", {"key": "model", "old": "sonnet", "new": "opus", **OWNER})):
            spool.put(box, kind, None, data, src="cli")
            self.hub.ingest()
        self.assertEqual(self.hub.state["config_seen"]["values"]["model"], "sonnet")
        self.assertEqual(self.facts(), [])
        refused = [e["data"]["type"] for e in journal.read(self.hub.journal.path) if e["type"] == "spool.refused"]
        self.assertEqual(refused, ["config.seen", "config.changed_outside"])


if __name__ == "__main__":
    unittest.main()
