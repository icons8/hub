"""The Russian owner's path through the modules while the tests speak English (tests/__init__.py HUB_LANG=en): each
place where a module picks the language itself — the owner's language of the process (i18n.owner/current), the page's
request language (the owner in the journal, hub.json), a language parameter — once more for a Russian owner, and once
for an English one, so the test proves the choice and not a constant. The expected text comes from the catalog
(`i18n.tr("ru", key, …)`) and the Russian input from the language data (tests/test_ru_owner.py russian_text), so the
source holds no Cyrillic. The core, the CLI, Telegram's commands and the page itself are in tests/test_ru_owner.py."""
import tests  # noqa: F401 — keeps the machine's settings out
import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import platforms
from core import i18n, journal, modrun, spool
from tests.test_ru_owner import ProcessLanguage, ReadySetup, russian_text, stand_down
from tests.fakes import cut

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_TS = os.path.join(HERE, "fakes", "fake_tailscale.py")


def temp_dir(case, prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    case.addCleanup(platforms.current.rmtree, d)
    return d


# --- browser: the supervisor's status lines and the reset reason (modules/browser/run.py) --------------------------

class BrowserProc:
    pid = 4242

    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0


class Browser(ProcessLanguage, unittest.TestCase):
    def supervisor(self, up=None, disk=None, elsewhere=None):
        from modules.browser import run
        self.state = temp_dir(self, "ru-browser-")
        self.now = [0.0]
        sup = run.Supervisor(self.state, clock=lambda: self.now[0], probe=lambda url: up)
        sup.reap = lambda url, now: False
        sup.disk_version = lambda cfg, b=None: disk
        sup.launch = lambda cfg, state_dir, name, b: BrowserProc()
        sup.kill_tree = lambda proc: setattr(proc, "alive", False)
        sup.kill_pid = lambda pid: None
        sup.running = lambda profile: elsewhere
        return sup

    def at(self, sup, t, b):
        self.now[0] = t
        return sup.check({}, "mac", b)["problem"]

    def lines(self, lang):
        """The supervisor's lines in one language: remote silent, launched, still starting, a foreign Chrome on the
        profile starting and then silent past the grace, and an update on disk (its reset reason in the spool)."""
        from modules.browser import run
        self.speak(lang)
        local = {"kind": "local", "url": "http://127.0.0.1:9", "port": 9, "risk": []}
        out = [self.at(self.supervisor(), 0, {"kind": "remote", "url": "http://127.0.0.1:9", "risk": []})]
        sup = self.supervisor()
        out += [self.at(sup, 0, local), self.at(sup, 1, local)]
        sup = self.supervisor(elsewhere=777)
        out += [self.at(sup, 0, local), self.at(sup, run.LAUNCH_GRACE + 1, local)]
        sup = self.supervisor(up="HeadlessChrome/154.0.8037.93", disk="154.0.8037.98")
        out.append(self.at(sup, 0, local))
        out += [e["data"]["why"] for _, e in spool.take(os.path.join(self.state, "spool")) if e["type"] == "browser.reset"]
        return out

    def expected(self, lang):
        return [i18n.tr(lang, "cli.browser.no_cdp"), i18n.tr(lang, "cli.browser.starting"),
                i18n.tr(lang, "cli.browser.starting"), i18n.tr(lang, "cli.browser.starting"),
                i18n.tr(lang, "cli.browser.profile_running_no_cdp", pid=777),
                i18n.tr(lang, "cli.browser.restart_updated"),
                i18n.tr(lang, "cli.browser.reset_updated", old="154.0.8037.93", new="154.0.8037.98")]

    def test_the_supervisor_states_its_browsers_in_the_owners_language(self):
        self.assertEqual(self.lines("ru"), self.expected("ru"))
        self.assertEqual(self.lines("en"), self.expected("en"))
        self.assertNotEqual(self.expected("ru"), self.expected("en"))


# --- directions: a decision the session took on its own, into the board (modules/directions/direction.py) ---------

class Directions(ProcessLanguage, unittest.TestCase):
    def record(self, lang):
        from modules.directions import direction
        self.speak(lang)
        path = temp_dir(self, "ru-direction-")
        os.makedirs(os.path.join(path, "backlog"))
        target = direction.record_decision(path, 7, "Keep the cache for a day", "maps", 1_700_000_000)
        with open(target, encoding="utf-8") as f:
            return f.read()

    def test_the_recorded_decision_is_in_the_owners_language(self):
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(1_700_000_000))
        for lang in ("ru", "en"):
            want = i18n.tr(lang, "directions.decision.recorded_context", by="maps", when=when, mark="[decided:7]")
            self.assertIn(want, self.record(lang), lang)
        self.assertNotIn(i18n.tr("ru", "directions.decision.recorded_context", by="maps", when=when, mark="[decided:7]"),
                         self.record("en"))


# --- files: the refusals of "Files" in the person's language (modules/files/server.py say) -------------------------

class Files(unittest.TestCase):
    def files(self, cfg):
        from modules.files.server import Files as Server
        state = temp_dir(self, "ru-files-")
        return Server(config=lambda: cfg, state_dir=state)

    def test_a_refusal_speaks_the_hub_language(self):
        ru = self.files({"language": "ru"})
        self.assertEqual(ru.read("nowhere", "a.md"), (404, {"error": i18n.tr("ru", "files.no_file")}))
        self.assertEqual(ru.listing("nowhere", ""), (404, {"error": i18n.tr("ru", "files.no_folder")}))
        en = self.files({})
        self.assertEqual(en.read("nowhere", "a.md"), (404, {"error": i18n.tr("en", "files.no_file")}))


# --- secrets: the reason of a refusal in the journal fact, for the owner (modules/secrets/engine.py) ---------------

ORIGIN = "https://login.example.com"


class Secrets(ProcessLanguage, unittest.TestCase):
    def facts(self, lang, item):
        from modules.secrets import engine
        self.speak(lang)
        cfg = {"secrets": {"browser": "agents"}, "browsers": {"agents": {"kind": "remote", "url": "http://127.0.0.1:9"}}}
        kept = {"site": {"name": "site", "sites": [ORIGIN], "login": False, "commands": False, "note": "", "value": True}}
        store = type("Store", (), {"meta": lambda self, name: kept.get(name), "value": lambda self, name, field: "x"})()
        facts = []
        e = engine.Engine(lambda: cfg, lambda kind, who, data: facts.append((kind, data["why"])), store=store,
                          targets=lambda url: [])   # no tab of the site is open
        e.fill("maps", item, "password")
        return facts

    def test_the_refusal_reason_is_in_the_owners_language(self):
        for lang in ("ru", "en"):
            self.assertEqual(self.facts(lang, "site"),
                             [("secret.denied", i18n.tr(lang, "cli.secrets.no_tab", origins=ORIGIN))], lang)
            self.assertEqual(self.facts(lang, "bank"), [("secret.denied", i18n.tr(lang, "cli.secrets.not_listed"))], lang)


# --- tailnet: why the tailnet is not up, in tailnet.json for the Modules tab (modules/tailnet/run.py) --------------

STATUS = {"BackendState": "Running",
          "Self": {"DNSName": "mac.tail.ts.net.", "TailscaleIPs": ["100.9.9.9"], "UserID": 3, "Tags": ["tag:server"]},
          "User": {"3": {"LoginName": "tagged-devices"}}}


class Tailnet(ProcessLanguage, unittest.TestCase):
    def reasons(self, lang):
        from modules.tailnet.run import Tailnet as Run
        self.speak(lang)
        d = temp_dir(self, "ru-tailnet-")
        fake = os.path.join(d, "ts.json")
        env = mock.patch.dict(os.environ, FAKE_TS_STATE=fake, FAKE_TS_LOG=os.path.join(d, "ts.log"))
        env.start()
        self.addCleanup(env.stop)
        cfg = {"tailnet": {"enabled": True}}   # off until turned on
        t = Run(d, config=lambda: cfg, cli=[sys.executable, FAKE_TS])
        out = []
        for status in ({**STATUS, "BackendState": "Stopped"}, STATUS):   # not connected; a tagged node with no owner
            with open(fake, "w") as f:
                json.dump({"status": status, "serve": {}}, f)
            out.append(t.tick()["why"])
        cfg["tailnet"] = {"enabled": False}
        out.append(t.tick()["why"])
        return out

    def test_the_reason_is_in_the_owners_language(self):
        for lang in ("ru", "en"):
            self.assertEqual(self.reasons(lang), [i18n.tr(lang, "cli.tailnet.not_connected", state="Stopped"),
                                                  i18n.tr(lang, "cli.tailnet.no_owner"),
                                                  i18n.tr(lang, "cli.tailnet.off")], lang)


# --- telegram: an attachment that did not download (modules/telegram/bot.py attach_reason) -------------------------

class Telegram(ProcessLanguage, unittest.TestCase):
    def failed(self, lang):
        from modules.telegram import bot as tg

        class Api:
            def download(self, file_id):
                if file_id == "net":
                    raise OSError("timed out")
                raise tg.TgError("Bad Request: wrong file_id")

        self.speak(lang)
        bot = tg.Bot(temp_dir(self, "ru-tg-"), Api())
        out = []
        for fid in ("net", "bad"):
            got, failed = bot.attachments({"message_id": 5, "document": {"file_id": fid, "file_name": "plan.pdf"}})
            self.assertEqual(got, [])
            out += [f["reason"] for f in failed]
        return out

    def test_the_reason_is_in_the_owners_language(self):
        for lang in ("ru", "en"):
            self.assertEqual(self.failed(lang), [
                i18n.tr(lang, "telegram.attach_network", file="plan.pdf", error="OSError: timed out"),
                i18n.tr(lang, "telegram.attach_error", file="plan.pdf", error="Bad Request: wrong file_id")], lang)


# --- the page: module endpoints, the app manifest, Settings, a manual account switch (modules/web) -----------------

class Page(unittest.TestCase):
    """The page server of a hub whose owner writes Russian (his message in the journal), and of one whose owner writes
    English; the browser asks for English both times, so the owner decides. HUB_LANG is off: it comes before the browser."""

    def setUp(self):
        self.addCleanup(i18n.use, "en")   # the tests' language back last, after every stand is down (stand_down)
        self.down = None

    def open(self, lang):
        from modules.web.server import Web
        if self.down:
            self.down()   # one stand at a time: two Tails would both set the process language
        self.state = temp_dir(self, f"ru-web-{lang}-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        j.append("message.received", None, {"text": russian_text() if lang == "ru" else "Please keep the cache for a day",
                                            "channel": "web", "owner": True})
        j.append("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})
        j.append("session.started", "hub", {"session_id": "h"})
        j.append("spool.mode", None, {"mode": "refuse", "set": "auto"})
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HUB_LANG", None)
        web = Web(self.state)
        web.decide_wait = 0
        web._setup = ReadySetup()
        server = web.serve(port=0)
        self.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.down = lambda: stand_down(web, server)
        self.addCleanup(self.down)   # after its folder and env cleanups in the order: runs before them
        web.tail.poll()
        return web

    def req(self, method, path, body=None, hub=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": f"127.0.0.1:{self.port}", "Content-Type": "application/json", "Accept-Language": "en",
             **({"X-Hub": "1"} if hub else {})}
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        data = r.read().decode("utf-8")
        c.close()
        return r.status, json.loads(data)

    def test_a_module_endpoint_refuses_in_the_page_language(self):
        for lang in ("ru", "en"):
            self.open(lang)
            os.makedirs(modrun.run_dir(self.state), exist_ok=True)
            modrun._write(os.path.join(modrun.run_dir(self.state), "fake.want.json"),
                          {"name": "fake", "cmd": ["true"], "cwd": self.state, "version": "1.0.0", "order": 0,
                           "budget": {}, "capabilities": ["http"], "key": "k"})
            self.assertEqual(self.req("GET", "/api/mod/fake/x", hub=False),
                             (403, {"error": i18n.tr(lang, "web.srv.err_not_from_page")}), lang)

    def test_the_app_manifest_names_the_hub_in_the_owners_language(self):
        for lang in ("ru", "en"):
            self.open(lang)
            self.assertEqual(self.req("GET", "/manifest.webmanifest")[1]["name"], i18n.tr(lang, "web.hub.default_title"))

    def test_settings_speak_the_owners_language(self):
        for lang in ("ru", "en"):
            self.open(lang)
            rows = {r["key"]: r for r in self.req("GET", "/api/snapshot")[1]["settings"]}
            self.assertEqual(rows["executors.agents.hub.model"]["label"],
                             i18n.tr(lang, "web.settings.executors_agent_model", agent="hub"), lang)
            self.assertEqual(rows["admit.mode"]["names"]["auto"], i18n.tr(lang, "web.settings.admit_auto_refuse"), lang)
            self.assertEqual(self.req("POST", "/api/settings/set", {"key": "no.such.key", "value": 1}),
                             (400, {"error": i18n.tr(lang, "web.settings.err_not_allowed")}), lang)

    @cut.needs("profile")
    def test_a_manual_account_switch_gives_its_reason_in_the_owners_language(self):
        pool = {"accounts": [{"name": "main", "token": "tok-m-0123456789"}, {"name": "work", "token": "tok-a-0123456789"}],
                "switch_at": 0.95}
        for lang in ("ru", "en"):
            self.open(lang)
            i18n.use(lang)   # the fact's reason is the process's owner language (i18n.owner), the hub's process
            self.addCleanup(i18n.use, "en")
            with mock.patch("modules.accounts.accounts.load", return_value=pool):
                code, _ = self.req("POST", "/api/accounts/use", {"account": "work"})
            self.assertEqual(code, 200)
            (fact,) = [e for _, e in spool.take(os.path.join(self.state, "spool")) if e["type"] == "account.switched"]
            self.assertEqual(fact["data"]["why"], i18n.tr(lang, "web.srv.picked_manually"), lang)


if __name__ == "__main__":
    unittest.main()
