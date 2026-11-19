"""A new hub greets its owner first (owner 2026-11-09: on a fresh hub with Codex the hub session said nothing until the
owner wrote «hi»; the introduction by hub-home/ONBOARDING.md waited for their first word). Once the hub session has
come up, the owner has come (their page opened: owner.came) and nobody has written, the core records owner.invited
and the fold gives the hub session one message to introduce itself, in the owner's language — the one their page
said, not the machine's (owner 2026-11-19: a Russian browser on an English system was greeted in English); once for the
whole journal, the first word after it is no second tour, on Claude and on Codex alike."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

import platforms

from core import journal, reducer, spool, system
from core.daemon import Hub
from hosts.codex import CodexHost
from hosts.streamjson import StreamHost
from tests import test_web

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

    def came(self, lang="ru", **who):
        return self.ev("owner.came", None, lang=lang, **(who or OWNER))

    def test_not_before_the_hub_session_has_come_up(self):
        self.came()
        self.assertFalse(system.invite_due(self.st))
        self.ev("session.started", "hub")
        self.assertTrue(system.invite_due(self.st))

    def test_not_before_the_owner_has_come(self):
        # the machine's language is not theirs: the hub waits for their page, however long
        self.ev("session.started", "hub")
        self.assertFalse(system.invite_due(self.st))
        self.assertIsNone(self.came("ru"))
        self.assertEqual(self.st["owner"]["came"], {"ts": self.st["owner"]["came"]["ts"], "lang": "ru", "via": "web"})
        self.assertTrue(system.invite_due(self.st))

    def test_the_first_coming_is_kept_once_and_only_the_owners(self):
        self.ev("session.started", "hub")
        self.assertIsNotNone(self.came("de", channel="session", sender="hub"))
        self.assertNotIn("came", self.st["owner"])
        self.came("de")
        self.came("ru")   # a second page, another browser
        self.assertEqual(self.st["owner"]["came"]["lang"], "de")
        self.came("tlh")
        self.assertEqual(self.st["owner"]["came"]["lang"], "de")

    def test_a_language_of_no_catalog_is_no_language(self):
        self.ev("session.started", "hub")
        self.came("tlh")
        self.assertIsNone(self.st["owner"]["came"]["lang"])
        self.assertTrue(system.invite_due(self.st))   # they came all the same: the hub greets them in the hub's language

    def test_a_hub_in_use_keeps_no_coming(self):
        self.ev("session.started", "hub")
        self.st["owner"]["seen"] = {"hub": 5}   # the owner read the hub before this version
        self.came()
        self.assertNotIn("came", self.st["owner"])
        self.assertFalse(system.invite_due(self.st))
        self.st["owner"].pop("seen")
        self.st["owner"]["greeted"] = True   # or wrote to it
        self.came()
        self.assertNotIn("came", self.st["owner"])

    def test_the_page_that_shows_the_hub_after_coming_does_not_cancel_the_greeting(self):
        # the page opens on the hub's chat and marks it read at once: that is no word of theirs
        self.ev("session.started", "hub")
        self.came()
        self.ev("owner.seen", "hub", seq=3, **OWNER)
        self.assertTrue(system.invite_due(self.st))
        self.assertIsNone(self.invite())
        self.assertEqual(len(self.msgs()), 1)
        self.assertFalse(system.invite_due(self.st))

    def test_the_invitation_is_one_core_message_to_the_hub_in_the_owners_language(self):
        self.ev("session.started", "hub")
        self.came()
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
        self.came()
        self.ev("message.received", None, text="Hello", **OWNER)
        self.assertFalse(system.invite_due(self.st))
        self.invite()
        self.assertEqual([m["text"] for m in self.msgs()], ["Hello"])

    def test_an_owner_who_came_and_writes_before_the_greeting_gets_the_introduction(self):
        # their page marked the hub's chat read; the hub session was still coming up (a download, a sign-in)
        self.came()
        self.ev("owner.seen", "hub", seq=1, **OWNER)
        self.ev("message.received", None, text="Hello", **OWNER)
        self.assertIn("first introduce the owner to the hub following ONBOARDING.md", self.msgs()[-1]["origin"])
        self.ev("session.started", "hub")
        self.assertFalse(system.invite_due(self.st))   # the introduction is their message's: no second one

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
        self.came()
        self.ev("turn.started", "hub", marker="m-old")
        self.assertFalse(system.invite_due(self.st))

    def test_the_session_is_the_one_the_fold_writes_to_not_one_by_a_name(self):
        self.ev("session.started", "hub")
        self.came()
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

    def test_the_greeting_speaks_the_language_the_page_said_not_the_machines(self):
        import types
        h = Hub(tempfile.mkdtemp(prefix="invite-l-"), types.SimpleNamespace())
        self.addCleanup(platforms.current.rmtree, h.state_dir)
        h.config = {**h.config, "hub": {"enabled": True, "name": "hub", "cwd": None}}
        h.config.pop("language", None)
        h.settings_known = True
        h.state["owner"]["came"] = {"ts": 1, "lang": "ru", "via": "web"}
        sent = []
        h.record = lambda t, e=None, d=None, **k: sent.append((t, d))
        with mock.patch.object(system, "invite_off", return_value=False), \
                mock.patch.object(system, "invite_due", return_value=True), \
                mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "HUB_LANG": "en"}):
            h.invite_owner()
        self.assertEqual(sent, [("owner.invited", {"lang": "ru", "channel": "core"})])
        h.config["language"] = "de"   # the hub's own language still comes first
        h.invite_tried = False
        with mock.patch.object(system, "invite_off", return_value=False), \
                mock.patch.object(system, "invite_due", return_value=True):
            h.invite_owner()
        self.assertEqual(sent[-1], ("owner.invited", {"lang": "de", "channel": "core"}))


class Page(unittest.TestCase):
    """The owner's page says they came, once, with the browser's language."""
    tearDown, req, spooled = test_web.WebTest.tearDown, test_web.WebTest.req, test_web.WebTest.spooled

    def setUp(self):
        test_web.WebTest.setUp(self)
        self.web.came_told = False

    def came(self):
        return [e for e in self.spooled() if e["type"] == "owner.came"]

    def test_the_first_page_says_who_came_and_in_what_language(self):
        code, _ = self.req("GET", "/", headers={"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
        self.assertEqual(code, 200)
        (fact,) = self.came()
        self.assertEqual(fact["data"]["lang"], "ru")
        self.assertTrue(fact["data"]["owner"])
        self.req("GET", "/", headers={"Accept-Language": "de"})   # once per page process: the fold keeps the first
        self.assertEqual(self.came(), [])

    def test_a_probe_of_the_port_is_nobody(self):
        # `hub instance` checks a new hub is up with a bare GET: no browser, no person
        code, _ = self.req("GET", "/")
        self.assertEqual(code, 200)
        self.assertEqual(self.came(), [])

    def test_a_hub_that_has_met_its_owner_hears_nothing(self):
        self.web.tail.lang_state = {"owner": {"lang": None, "came": None, "met": True}, "ui_lang_default": None}
        self.req("GET", "/", headers={"Accept-Language": "ru"})
        self.assertEqual(self.came(), [])


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
        self.come()
        self.hub = self.new_hub()
        self.hub.start()
        self.addCleanup(self.stop)

    def come(self, lang="de"):
        """The owner's page opened (modules/web Web.came): the browser's language; hub.json's language comes first."""
        spool.put(os.path.join(self.state, "spool"), "owner.came", None, {"lang": lang, "channel": "web", "owner": True})

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

    def test_an_owner_who_has_not_come_is_not_greeted_and_one_who_comes_is(self):
        self.stop()
        platforms.current.rmtree(self.state)
        os.makedirs(self.state)
        with open(self.cfg, "w") as f:   # no language of the hub's own: the page's one decides
            json.dump({"update": {"enabled": False}, "hub": {"enabled": True, "name": "hub", "cwd": self.state}}, f)
        self.hub = self.new_hub()
        self.hub.start()
        self.until(lambda: self.hub.state["sessions"].get("hub", {}).get("ever_started"))
        for _ in range(10):
            self.hub.tick()
            time.sleep(0.03)
        self.assertEqual(self.events("owner.invited"), [])
        self.come("de")
        self.until(lambda: self.events("owner.invited"))
        (fact,) = self.events("owner.invited")
        self.assertEqual(fact["data"]["lang"], "de")
        self.until(lambda: self.invitations() and self.invitations()[0]["state"] == "delivered")
        self.assertIn("Write in German", self.invitations()[0]["text"])

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
