"""A new hub greets its owner first (owner 2026-11-09: on a fresh hub with Codex the hub session said nothing until the
owner wrote «hi»; the introduction by hub-home/ONBOARDING.md waited for their first word). Once the hub session has
come up and nobody has written, the core records owner.invited and the fold gives the hub session one message to
introduce itself, in the owner's language; once for the whole journal, the first word after it is no second tour, on
Claude and on Codex alike."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms

from core import i18n, journal, reducer, spool, system
from core.daemon import Hub
from hosts.codex import CodexHost
from hosts.streamjson import StreamHost

OWNER = {"channel": "web", "owner": True}
HERE = os.path.dirname(__file__)
FAKE_CLAUDE = os.path.join(HERE, "fakes", "fake_claude_stream.py")
FAKE_CODEX = os.path.join(HERE, "fakes", "fake_codex.py")


class Fold(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def msgs(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub"]

    def invite(self, lang="ru"):
        return self.ev("owner.invited", None, lang=lang, channel="core")

    def test_not_before_the_hub_session_has_come_up(self):
        self.assertFalse(system.invite_due(self.st))
        self.ev("session.started", "hub")
        self.assertTrue(system.invite_due(self.st))

    def test_the_invitation_is_one_core_message_to_the_hub_in_the_owners_language(self):
        self.ev("session.started", "hub")
        self.assertIsNone(self.invite())
        (msg,) = self.msgs()
        self.assertEqual(msg["from"], {"kind": "core"})
        self.assertIn("ONBOARDING.md", msg["text"])
        self.assertIn("Write in Russian", msg["text"])
        self.assertIn("the address book step waits for their first word", msg["text"])
        self.assertNotIn("from_owner", msg)
        self.assertEqual(msg["state"], "typing")   # an idle session takes it at once: a turn nobody asked for
        self.assertTrue(any(a.get("do") == "type" and a["session"] == "hub" for a in self.acts))
        self.assertFalse(system.invite_due(self.st))

    def test_the_fact_twice_is_one_message(self):
        self.ev("session.started", "hub")
        self.invite()
        self.invite("de")
        self.assertEqual(len(self.msgs()), 1)

    def test_the_owner_who_wrote_first_is_not_invited(self):
        self.ev("session.started", "hub")
        self.ev("message.received", None, text="Hello", **OWNER)
        self.assertFalse(system.invite_due(self.st))
        self.invite()
        self.assertEqual([m["text"] for m in self.msgs()], ["Hello"])

    def test_the_first_word_after_the_invitation_is_no_second_tour(self):
        self.ev("session.started", "hub")
        self.invite()
        self.ev("message.received", None, text="Hello", **OWNER)
        hello = self.msgs()[-1]
        self.assertEqual(hello["text"], "Hello")
        # no second tour, but the address book step is still to be done, on their first word
        self.assertIn("the first message, you have greeted them already", hello["origin"])
        self.assertIn("address book", hello["origin"])
        self.assertNotIn("first introduce", hello["origin"])
        self.ev("message.received", None, text="And more", **OWNER)
        self.assertNotIn("the first message", self.msgs()[-1]["origin"])   # only the first word carries it

    def test_a_hub_that_was_in_use_before_this_version_is_not_invited(self):
        self.ev("session.started", "hub")
        self.st["owner"]["seen"] = {"hub": 5}
        self.assertFalse(system.invite_due(self.st))

    def test_a_hub_session_that_has_had_a_turn_is_no_new_hub(self):
        # the owner's words may be gone from the state (a long-lived hub, trimmed messages): the hub's own turns say so
        self.ev("session.started", "hub")
        self.ev("turn.started", "hub", marker="m-old")
        self.assertFalse(system.invite_due(self.st))

    def test_the_session_is_the_one_the_fold_writes_to_not_one_by_a_name(self):
        self.ev("session.started", "hub")
        self.assertTrue(system.invite_due(self.st))
        self.st["sessions"]["hub"]["state"] = "closed"   # no live hub session: nothing is due
        self.assertFalse(system.invite_due(self.st))

    def test_only_the_core_invites(self):
        self.ev("session.started", "hub")
        self.assertIsNotNone(self.ev("owner.invited", None, lang="ru", **OWNER))
        self.assertEqual(self.msgs(), [])

    def test_no_hub_session_no_message_and_no_mark(self):
        self.st["sessions"].pop("hub")   # a hub with its session switched off (hub.json hub.enabled)
        self.invite()
        self.assertNotIn("invited", self.st["owner"])
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h2", kind="hub", **OWNER)
        self.assertTrue(reducer.first_word(self.st))
        self.assertEqual(self.msgs(), [])

    def test_a_language_of_no_catalog_is_named_as_it_is(self):
        self.ev("session.started", "hub")
        self.invite("it")
        self.assertIn("Write in it.", self.msgs()[0]["text"])

    def test_the_system_language_is_a_ready_one_or_none(self):
        for tag, want in (("ru-RU", "ru"), ("en-US", "en"), ("pt-PT", "pt-BR"), ("", None), ("tlh", None)):
            with mock.patch.object(platforms.current, "system_language", return_value=tag):
                self.assertEqual(i18n.system_language(), want, tag)


class SystemLanguage(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX adapter")
    def test_a_mac_asks_its_settings_and_others_the_environment(self):
        from unittest import mock as m
        from platforms import posix
        done = m.Mock(stdout='(\n    "ru-RU",\n    "en-US"\n)\n', returncode=0)
        with m.patch.object(posix.sys, "platform", "darwin"), m.patch.object(posix.subprocess, "run", return_value=done):
            self.assertEqual(posix.system_language(), "ru-RU")
        with m.patch.object(posix.sys, "platform", "linux"), \
                m.patch.dict(os.environ, {"LC_ALL": "", "LC_MESSAGES": "", "LANG": "de_DE.UTF-8"}):
            self.assertEqual(posix.system_language(), "de-DE")
        with m.patch.object(posix.sys, "platform", "linux"), m.patch.dict(os.environ, {"LC_ALL": "C", "LANG": "ru_RU"}):
            self.assertEqual(posix.system_language(), "ru-RU")   # a stray C does not hide the language LANG names
        with m.patch.object(posix.sys, "platform", "linux"), m.patch.dict(os.environ, {"LC_ALL": "C", "LANG": "C.UTF-8"}):
            self.assertEqual(posix.system_language(), "")   # C says nothing
        failed = m.Mock(stdout="The domain/default pair does not exist\n", returncode=1)
        with m.patch.object(posix.sys, "platform", "darwin"), m.patch.object(posix.subprocess, "run", return_value=failed), \
                m.patch.dict(os.environ, {"LC_ALL": "", "LC_MESSAGES": "", "LANG": "es_ES.UTF-8"}):
            self.assertEqual(posix.system_language(), "es-ES")   # an error text is no language
        with m.patch.object(posix.sys, "platform", "darwin"), m.patch.object(posix.subprocess, "run", side_effect=OSError), \
                m.patch.dict(os.environ, {"LC_ALL": "", "LC_MESSAGES": "", "LANG": "fr_FR.UTF-8"}):
            self.assertEqual(posix.system_language(), "fr-FR")   # defaults missing: the environment

    def test_both_adapters_have_it(self):
        from platforms import posix, windows
        self.assertTrue(callable(posix.system_language) and callable(windows.system_language))


class Daemon(unittest.TestCase):
    def test_a_fact_the_fold_took_no_effect_from_is_not_written_every_tick(self):
        import types
        h = Hub(tempfile.mkdtemp(prefix="invite-t-"), types.SimpleNamespace())
        h.config = {**h.config, "hub": {"enabled": True, "name": "hub", "cwd": None}}
        h.settings_known = True
        sent = []
        h.record = lambda t, e=None, d=None, **k: sent.append((t, d))
        with mock.patch.object(system, "invite_off", return_value=False), mock.patch.object(system, "invite_due", return_value=True):
            for _ in range(3):
                h.invite_owner()
        self.assertEqual([t for t, _ in sent], ["owner.invited"])


class OnClaude(unittest.TestCase):
    """The daemon with a fake host: the fact once, the message delivered once, a restart brings no second."""
    HOST = "claude"

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="invite-")
        self.cfg = os.path.join(self.state, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({"update": {"enabled": False}, "hub": {"enabled": True, "name": "hub", "cwd": self.state},
                       "language": "ru"}, f)
        env = {k: v for k, v in os.environ.items() if k != "HUB_NO_INVITE"}
        self.patch = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        self.patch.start()
        os.environ.pop("HUB_NO_INVITE", None)
        self.addCleanup(self.patch.stop)
        self.addCleanup(platforms.current.rmtree, self.state)
        self.hub = self.new_hub()
        self.hub.start()
        self.addCleanup(self.stop)

    def host(self, env):
        return StreamHost(self.state, claude=(sys.executable, FAKE_CLAUDE), env=env)

    def new_hub(self):
        host = self.host({"FAKE_TURN_S": "0.1"})
        return Hub(self.state, host, registry_dir=os.path.join(self.state, "registry"))

    def stop(self):
        kill = getattr(self.hub.host, "kill_server", None)
        if kill:
            kill()

    def until(self, pred, timeout=15.0):
        end = time.time() + timeout
        while time.time() < end:
            self.hub.tick()
            if pred():
                return
            time.sleep(0.03)
        self.fail(f"did not wait; {self.hub.state['sessions']} {self.hub.state['messages']}")

    def events(self, type, entity=None):
        return [e for e in journal.read(self.hub.journal.path) if e["type"] == type
                and (entity is None or e.get("entity") == entity)]

    def invitations(self):
        return [m for m in self.hub.state["messages"].values() if "ONBOARDING.md" in str(m["text"])]

    def test_first_start_one_invitation_delivered_and_answered_restart_no_second(self):
        self.until(lambda: self.events("session.said", "hub"))
        (fact,) = self.events("owner.invited")
        self.assertEqual(fact["data"]["lang"], "ru")
        (msg,) = self.invitations()
        self.assertEqual(msg["state"], "delivered")
        self.assertIn("Write in Russian", msg["text"])
        said = [t["data"]["text"] for t in self.events("session.said", "hub")]
        self.assertTrue(any("The hub has started for the first time" in t for t in said), said)
        self.stop()
        for _ in range(3):
            self.hub.tick()
        # a restart: a new daemon over the same journal and the same state folder
        self.hub = self.new_hub()
        self.hub.start()
        for _ in range(5):
            self.hub.tick()
            time.sleep(0.05)
        self.assertEqual(len(self.events("owner.invited")), 1)
        self.assertEqual(len(self.invitations()), 1)

    def test_an_owner_who_writes_before_the_hub_has_come_up_gets_no_invitation(self):
        spool.put(os.path.join(self.state, "spool"), "message.received", None,
                  {"to": "hub", "text": "hi", "channel": "web", "owner": True})
        self.until(lambda: self.events("session.said", "hub"))
        self.assertEqual(self.events("owner.invited"), [])
        self.assertEqual(self.invitations(), [])


class OnCodex(OnClaude):
    """The same on a hub entirely on Codex: the message is an ordinary one for the host."""

    def host(self, env):
        return CodexHost(self.state, codex=(sys.executable, FAKE_CODEX), env=env, all_sessions=True,
                         default_model="gpt-def", default_effort="low")


if __name__ == "__main__":
    unittest.main()
