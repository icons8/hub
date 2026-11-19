"""The Russian owner's path while the tests speak English (tests/__init__.py HUB_LANG=en): the same families the other
tests check in English — `hub` output in the owner's terminal, Telegram, cards, the page — once more in Russian. The
expected text comes from the catalog (`i18n.tr("ru", key, …)`) and the Russian input from the language data
(tests/fakes/lang.py), so the source holds no Cyrillic; what is checked is that the code picks the owner's language and
fills the Russian strings, not the words themselves (tests/test_i18n.py keeps the catalogs whole). The page in Chrome
under Russian is tests/test_web_e2e.py RussianOwner."""
import tests  # noqa: F401 — keeps the machine's settings out
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import platforms
from core import i18n, journal, reducer
from modules.telegram import bot as tg
from modules.web.server import Web
from tests.fakes import lang
from tests.test_telegram import FakeBotApi, OWNER
from tests.test_web import ReadySetup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "bin", "hub")


def russian_text():
    """An owner's message the core takes for Russian: words of the language data until one carries a letter only
    Russian has (without it Cyrillic is Russian or Ukrainian — no language)."""
    own = lang.data("letters", "own")
    pool = [w for key in ("doubt", "promise_verbs") for w in lang.words("watch", key)]
    picked = [w for w in pool if any(c in own for c in w)][:1] + pool[:6]
    return " ".join(picked)


def stand_down(web, server):
    """A page stand down and its threads finished: its Tail follows the owner's language in this process (core/tail.py)
    and reads the state folder — the tests' language and the folder are given back only after it. Twice is harmless."""
    web.stop.set()
    server.shutdown()
    server.server_close()
    mine = {id(web), *(id(getattr(web, name, None)) for name in ("tail", "shown", "push"))}
    for th in threading.enumerate():
        if id(getattr(getattr(th, "_target", None), "__self__", None)) in mine:
            th.join(10)


class ProcessLanguage:
    """A mixin for the sites that take the process language (i18n.current, i18n.owner): a hub whose owner writes Russian
    speaks it in its processes; `speak(lang)` switches it for one test and the tests' language comes back after it."""

    def speak(self, lang):
        i18n.use(lang)
        self.addCleanup(i18n.use, "en")


class Russian(unittest.TestCase):
    """The process speaks the Russian owner's language, as a hub whose owner writes Russian."""

    def setUp(self):
        i18n.use("ru")
        self.addCleanup(i18n.use, "en")


class Data(unittest.TestCase):
    def test_the_owner_message_of_the_data_is_russian(self):
        st, _, why = reducer.reduce(reducer.empty(), {"seq": 1, "ts": 1, "type": "message.received", "entity": None,
                                                     "data": {"text": russian_text(), "channel": "web", "owner": True}})
        self.assertIsNone(why)
        self.assertEqual(i18n.state_lang(st), "ru")


class Catalog(unittest.TestCase):
    """Every Russian string renders with its own substitutions: the tests no longer draw most of them in Russian, so a
    form missing for a count or a substitution left in the text would reach the owner unseen."""

    def test_every_key_formats_with_its_placeholders_and_every_count(self):
        with open(os.path.join(ROOT, "i18n", "ru.json"), encoding="utf-8") as f:
            ru = json.load(f)
        need, _ = i18n.forms("ru")
        bad = []
        for key, value in ru.items():
            names = i18n.placeholders(value)
            for n in (0, 1, 2, 5, 11, 21, 22, 25, 101, 1.5):
                params = {name: f"<{name}>" for name in names}
                params["count"] = n
                clash = {k: params.pop(k) for k in ("lang", "key") if k in params}   # filled by the caller's own format
                got = i18n.format(i18n.tr("ru", key, **params), clash)
                if got == key or any("{" + name + "}" in got for name in names) or not got.strip():
                    bad.append((key, n, got))
            if isinstance(value, dict) and not need <= set(value):
                bad.append((key, "forms", sorted(need - set(value))))
        self.assertEqual(bad, [])


class Telegram(Russian):
    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp(prefix="hubtg-ru-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.state = os.path.join(self.dir, "state")
        env = mock.patch.dict(os.environ, {"HUB_TELEGRAM_CONFIG": os.path.join(self.dir, "telegram.json")})
        env.start()
        self.addCleanup(env.stop)
        tg.save_config({"token": "1:x", "owners": [OWNER]})
        self.fake = FakeBotApi()
        self.addCleanup(self.fake.server.server_close)
        self.addCleanup(self.fake.server.shutdown)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})
        self.j.append("session.started", "hub", {"session_id": "h"})
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.catch_up()

    def command(self, text, mid=50):
        self.fake.updates = [{"update_id": mid, "message": {"message_id": mid, "from": {"id": OWNER},
                                                            "chat": {"id": OWNER, "type": "private"}, "text": text}}]
        self.bot.poll(timeout=0)

    def test_stop_with_nobody_working(self):
        self.bot.tick()
        self.fake.calls.clear()
        self.command("/stop", mid=60)
        (reply,) = self.fake.sent()
        self.assertEqual(reply["text"], i18n.tr("ru", "telegram.stop_none"))

    def test_status_counts(self):
        self.j.append("turn.started", "hub", {"marker": None, "prompt_head": "busy"})
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "AskUserQuestion", "interactive": True,
                                                    "input": {"questions": [{"question": "Merge?", "options": [
                                                        {"label": "Yes"}]}]}})
        self.j.append("message.received", None, {"text": "and more", "channel": "web", "owner": True})
        self.bot.tick()
        self.fake.calls.clear()
        self.command("/status")
        (reply,) = self.fake.sent()
        self.assertIn(i18n.tr("ru", "telegram.status_questions", questions=1, queued=1), reply["text"])

    def test_the_permission_card_says_why_it_asks(self):
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("permission.requested", "w", {"request_id": "r1", "tool": "Bash", "input": {"command": "git push -f"},
                                                    "why": "the ask rule Bash(git push -f *)"})
        self.bot.tick()
        self.assertIn(i18n.tr("ru", "telegram.perm_why", why="the ask rule Bash(git push -f *)"),
                      self.fake.sent()[-1]["text"])

    def test_a_file_size_has_the_decimal_comma(self):
        # Russian writes 25,3 — the English tests see 25.3: the only place the branch runs
        size = int(25.3 * 2**20)
        got = tg.Bot.attach_reason("plan.pdf", size, "too_big", None)
        mb = i18n.tr("ru", "telegram.size_mb", n="25,3")
        self.assertEqual(got, i18n.tr("ru", "telegram.attach_too_big", limit=tg.MAX_FILE >> 20,
                                      file=i18n.tr("ru", "telegram.attach_file_size", name="plan.pdf", size=mb)))
        i18n.use("en")
        self.assertIn("25.3", tg.Bot.attach_reason("plan.pdf", size, "too_big", None))


class Cli(unittest.TestCase):
    """`hub` in the owner's terminal reads his language; a session's step reads English whatever the owner speaks."""

    def run_hub(self, *args, session=None):
        state = tempfile.mkdtemp(prefix="hub-ru-cli-")
        self.addCleanup(platforms.current.rmtree, state)
        cfg = os.path.join(state, "hub.json")
        with open(cfg, "w") as f:
            json.dump({"update": {"enabled": False}, "language": "ru"}, f)
        env = {k: v for k, v in os.environ.items() if k not in ("HUB_LANG", "HUB_SESSION")}
        env.update(HUB_STATE_DIR=state, HUB_CONFIG=cfg, HUB_CLI_WAIT="0", **({"HUB_SESSION": session} if session else {}))
        p = subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True, encoding="utf-8",
                           timeout=60)
        return p.stdout + p.stderr

    def test_the_owner_reads_his_language(self):
        self.assertIn(i18n.tr("ru", "cli.task.unknown"), self.run_hub("task", "show", "d999"))

    def test_the_commands_of_the_core_and_its_modules_read_his_language_too(self):
        # the module's CLI and the core's command families take the language of the `hub` process: one check each
        for args, key in ((("marketplace", "list"), "core.marketplaces.no_catalogs"),
                          (("direction", "list"), "directions.cli.no_directions")):
            want = i18n.tr("ru", key, root="")
            self.assertIn(want[:want.index("(")] if "(" in want else want, self.run_hub(*args), args)
        self.assertIn(i18n.tr("ru", "cli.modules.none", head="")[3:20], self.run_hub("modules"))

    def test_a_session_reads_english(self):
        out = self.run_hub("task", "show", "d999", session="maps")
        self.assertIn(i18n.tr("en", "cli.task.unknown"), out)
        self.assertNotIn(i18n.tr("ru", "cli.task.unknown"), out)


class Page(unittest.TestCase):
    """The page server for a Russian owner: the static text, the strings bundle and the snapshot's words."""

    def setUp(self):
        # the stand's Tail follows the owner's language in this process (core/tail.py): the tests' language comes back
        # last, after the stand is down
        self.addCleanup(i18n.use, "en")
        self.state = tempfile.mkdtemp(prefix="hubweb-ru-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("message.received", None, {"text": russian_text(), "channel": "web", "owner": True})
        self.j.append("session.requested", "maps", {"cwd": "/w", "session_id": "s1"})
        self.j.append("session.started", "maps", {"session_id": "s1"})
        self.web = Web(self.state)
        self.web.decide_wait = 0
        self.web._setup = ReadySetup()
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        # the language of a hub without a setting: the owner's, the tests' HUB_LANG would come before the browser
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HUB_LANG", None)
        self.addCleanup(stand_down, self.web, self.server)   # first of the cleanups: the stand is down before the rest
        self.web.tail.poll()

    def get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", "Accept-Language": "en"})
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body.decode("utf-8")

    def test_the_page_is_russian_for_a_russian_owner(self):
        status, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('<html lang="ru"', html)
        status, body = self.get("/i18n.json")
        bundle = json.loads(body)
        self.assertEqual(bundle["lang"], "ru")
        for key in ("web.cards.ask_head", "web.dock.label_session"):
            self.assertEqual(bundle["messages"][key], i18n.tr("ru", key))

    def test_the_snapshot_speaks_the_owners_language(self):
        snap = json.loads(self.get("/api/snapshot")[1])
        self.assertEqual(snap["owner"]["lang"], "ru")
        row = next(s for s in snap["sessions"] if s["name"] == "maps")
        self.assertEqual(row["label"], i18n.tr("ru", "core.views.state.idle"))


if __name__ == "__main__":
    unittest.main()
