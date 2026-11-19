"""Telegram owner pairing: the code is issued only in a private chat, is not overwritten, there are at most PAIR_MAX
of them and they live PAIR_TTL; `pair` names whom it paired (name and id); nobody overwrites another's settings file."""
import tests  # noqa: F401 — keeps the machine's settings out
import io
import os
import time
import unittest
from unittest import mock

from modules.telegram import bot as tg
from tests import test_telegram as base


class Pairing(unittest.TestCase):
    # helpers by copy, not inheritance: a subclass of TelegramTest would run all its tests again
    T = base.TelegramTest
    tearDown, msg = T.tearDown, T.msg

    def setUp(self):
        self.T.setUp(self)
        tg.save_config({"token": "1:x"})
        self.bot.cfg = tg.load_config()

    def start(self, user, mid, chat_type="private", name="Alex"):
        m = self.msg("/start", mid, user=user)
        m["message"]["chat"]["type"] = chat_type
        m["message"]["from"]["first_name"] = name
        self.fake.updates = [m]
        self.bot.poll(timeout=0)

    def pending(self):
        return tg.load_config().get("pending") or {}

    def pair(self, *args):
        with mock.patch.dict(os.environ, {"HUB_TELEGRAM_API": self.fake.base}):
            import importlib
            importlib.reload(tg)
            try:
                return tg.main(["pair", *args])
            finally:
                importlib.reload(tg)

    def test_only_a_private_chat_gets_a_code(self):
        # /start in a group with the bot gave a code to any group member
        for kind in ("group", "supergroup", "channel"):
            self.start(555, 1, chat_type=kind)
        self.assertEqual((self.pending(), self.fake.sent()), ({}, []))
        self.start(555, 2)
        self.assertEqual(len(self.pending()), 1)

    def test_the_same_person_keeps_one_code(self):
        self.start(555, 1)
        code = next(iter(self.pending()))
        self.start(555, 2)
        self.assertEqual(list(self.pending()), [code])
        self.assertIn(code, self.fake.sent()[-1]["text"])

    def test_a_code_is_never_overwritten(self):
        # a second /start got the same code and overwrote the first: the wrong person, not the one who passed the code, would pair
        with mock.patch("secrets.randbelow", side_effect=[123456, 123456, 654321]):
            self.start(555, 1)
            self.start(666, 2)
        pending = self.pending()
        self.assertEqual(pending["123456"]["user"], 555)
        self.assertEqual(pending["654321"]["user"], 666)

    def test_the_queue_is_bounded(self):
        for i in range(tg.PAIR_MAX + 5):
            self.start(1000 + i, i + 1)
        self.assertEqual(len(self.pending()), tg.PAIR_MAX)

    def test_strangers_do_not_lock_the_owner_out(self):
        # verify №16: six strangers' /start (the bot's name is all they need) held all the places for ten minutes
        self.bot.clock = lambda: 1000.0
        for i in range(tg.PAIR_MAX + 1):
            self.bot.clock = lambda i=i: 1000.0 + i
            self.start(1000 + i, i + 1)
        self.bot.clock = lambda: 1010.0
        self.start(555, 50, name="Owner")
        pending = self.pending()
        self.assertEqual(len(pending), tg.PAIR_MAX)
        code = next(c for c, v in pending.items() if v["user"] == 555)
        self.assertIn(code, self.fake.sent()[-1]["text"])
        self.assertNotIn(1000, [v["user"] for v in pending.values()])   # the oldest went
        self.assertNotIn(1001, [v["user"] for v in pending.values()])

    def test_a_schedule_job_does_not_pair(self):
        self.start(555, 1)
        code = next(iter(self.pending()))
        with mock.patch.dict(os.environ, {"HUB_SCHEDULE_JOB": "nightly"}), self.assertRaises(SystemExit):
            self.pair(code)
        self.assertEqual(tg.load_config().get("owners") or [], [])

    def test_old_codes_expire_and_make_room(self):
        self.bot.clock = lambda: 1000.0
        for i in range(tg.PAIR_MAX):
            self.start(1000 + i, i + 1)
        self.bot.clock = lambda: 1000.0 + tg.PAIR_TTL + 1
        self.start(2000, 99)
        self.assertEqual([v["user"] for v in self.pending().values()], [2000])

    def test_the_bot_does_not_wipe_what_another_process_wrote(self):
        cfg = tg.load_config()
        cfg["token"] = "2:new"
        tg.save_config(cfg)                         # `hub-telegram token` wrote it while the bot has an old copy
        self.start(555, 1)
        self.assertEqual(tg.load_config()["token"], "2:new")
        self.assertEqual(len(self.pending()), 1)

    def test_the_token_command_keeps_what_was_written_during_its_network_call(self):
        self.start(555, 1)

        def get_me(_self, method, **_):
            cfg = tg.load_config()
            cfg["pending"]["111111"] = {"user": 7, "name": "Sam", "at": time.time()}
            tg.save_config(cfg)                     # the bot wrote it while getMe was in flight
            return {"username": "test_bot"}
        with mock.patch.object(tg.Api, "call", get_me), mock.patch("sys.stdin", io.StringIO("9:token\n")), \
                mock.patch("sys.stdout", new=io.StringIO()):
            tg.main(["token"])
        cfg = tg.load_config()
        self.assertEqual(cfg["token"], "9:token")
        self.assertEqual(len(cfg["pending"]), 2)

    def test_pair_names_who_it_bound_and_clears_the_queue(self):
        self.start(555, 1, name="Mallory")
        self.start(666, 2)
        code = next(c for c, v in self.pending().items() if v["user"] == 555)
        with mock.patch("sys.stdout", new=io.StringIO()) as out:
            self.pair(code)
        self.assertIn("Mallory", out.getvalue())
        self.assertIn("555", out.getvalue())
        cfg = tg.load_config()
        self.assertEqual((cfg["owners"], cfg["pending"]), ([555], {}))

    def test_an_expired_code_does_not_pair(self):
        self.start(555, 1)
        cfg = tg.load_config()
        for v in cfg["pending"].values():
            v["at"] = time.time() - tg.PAIR_TTL - 5
        tg.save_config(cfg)
        with self.assertRaises(SystemExit):
            self.pair(next(iter(cfg["pending"])))
        self.assertEqual(tg.load_config().get("owners") or [], [])

    def test_an_old_style_code_counts_as_expired(self):
        # the old {code: id} format has no time: counted as expired
        tg.save_config({"token": "1:x", "pending": {"123456": 555}})
        with self.assertRaises(SystemExit):
            self.pair("123456")
        self.assertEqual(tg.load_config().get("owners") or [], [])

    def test_a_session_or_plugin_process_cannot_bind_the_owner_or_swap_the_bot(self):
        # `pair` and `token` change who the bot obeys: a word of the owner at the terminal, never of a session or a plugin
        self.start(555, 1, name="Mallory")
        code = next(iter(self.pending()))
        for var, value in (("HUB_SESSION", "maps"), ("HUB_EXTENSION", "demo"), ("HUB_MODULE", "web")):
            for args in (["pair", code], ["token"]):
                with self.subTest(var=var, cmd=args[0]), \
                        mock.patch.dict(os.environ, {var: value}), \
                        mock.patch("sys.stdin", io.StringIO("9:token\n")), \
                        mock.patch.object(tg.Api, "call", side_effect=AssertionError("reached Telegram")):
                    with self.assertRaises(SystemExit) as cm:
                        tg.main(args)
                    self.assertIn("terminal", str(cm.exception))
        cfg = tg.load_config()
        self.assertEqual((cfg.get("owners") or [], cfg["token"], list(cfg["pending"])), ([], "1:x", [code]))

    def test_outside_a_session_pair_and_token_work_as_before(self):
        self.start(555, 1)
        with mock.patch.dict(os.environ), mock.patch("sys.stdout", new=io.StringIO()):
            for var in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE"):
                os.environ.pop(var, None)
            self.pair(next(iter(self.pending())))
        self.assertEqual(tg.load_config()["owners"], [555])

    def test_the_settings_file_has_a_temporary_name_per_process(self):
        seen = []
        real = os.replace

        def replace(a, b):
            seen.append(a)
            return real(a, b)
        with mock.patch.object(tg.os, "replace", replace):
            tg.save_config({"token": "1:x"})
        self.assertEqual(seen, [f"{tg.config_path()}.{os.getpid()}.tmp"])


if __name__ == "__main__":
    unittest.main()
