"""Introduction on the first message (owner decision 2026-10-02: on the first greeting the hub onboards the user and
tells about all features so they can ask it questions): the core marks the owner's very first message to the hub,
and the hub gives a tour from hub-home/ONBOARDING.md by that mark."""
import tests  # noqa: F401 — hide the machine's settings
import os
import re
import unittest

from core import i18n, reducer
from modules.telegram import bot

OWNER = {"channel": "web", "owner": True}
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Onboarding(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.acts = reducer.empty(), 0, []
        self.ev("session.requested", "hub", cwd="/h", session_id="s-h", kind="hub", **OWNER)
        self.ev("session.started", "hub")

    def ev(self, type, entity=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": 100 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.acts += acts
        return why

    def msgs(self):
        return [m for m in self.st["messages"].values() if m["session"] == "hub"]

    def test_only_the_very_first_owner_message_is_marked(self):
        self.ev("message.received", None, text="Hello", **OWNER)
        self.ev("message.received", None, text="What can you do?", **OWNER)
        first, second = self.msgs()
        self.assertIn("the first message", first["origin"])
        self.assertIn("ONBOARDING.md", first["origin"])
        self.assertNotIn("the first message", second["origin"])

    def test_a_hub_with_history_is_not_onboarded_again(self):
        # a hub where the owner has already written (before this code version) gets no tour
        self.st["owner"]["seen"] = {"hub": 5}
        self.ev("message.received", None, text="Hello", **OWNER)
        self.assertNotIn("the first message", self.msgs()[0]["origin"])

    def test_the_tour_exists_and_the_role_knows_it(self):
        with open(os.path.join(ROOT, "hub-home", "ONBOARDING.md")) as f:
            tour = f.read()
        for part in ("Telegram", "Hub Developer", "Flow", "schedule", "EXTENDING.md"):
            self.assertIn(part, tour)
        with open(os.path.join(ROOT, "hub-home", "CLAUDE.md")) as f:
            self.assertIn("ONBOARDING.md", f.read())

    def test_telegram_is_connected_by_commands_that_exist_and_the_token_stays_out_of_the_chat(self):
        # "hub-telegram setup" has no such subcommand; the bot token is typed by the owner in their terminal, not
        # sent as a chat message
        with self.assertRaises(SystemExit) as raised:
            bot.main([])
        usage = str(raised.exception)
        real = set(re.findall(r"(?:^|[|\s])(token|pair|status|serve)\b", usage.split("\n\n")[-1]))
        self.assertGreaterEqual(real, {"token", "pair"})
        for name in ("README.md", os.path.join("hub-home", "ONBOARDING.md")):
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                text = f.read()
            for sub in re.findall(r"hub-telegram\s+([a-z]+)", text):
                self.assertIn(sub, real, f"{name}: hub-telegram has no subcommand «{sub}»")
        with open(os.path.join(ROOT, "hub-home", "ONBOARDING.md"), encoding="utf-8") as f:
            tour = f.read()
        self.assertIn("hub-telegram token", tour)
        self.assertIn("hub-telegram pair", tour)
        self.assertRegex(tour, r"(?i)token never goes into this chat")
        self.assertRegex(tour, r"/revoke")   # if it was sent anyway, the token is exposed: reissue it, do not reuse it
        self.assertNotRegex(tour, r"(?i)send me the token|paste the token here")
        for lang in ("en", "ru"):   # the module card hint on the page — the same path, without "token in telegram.json"
            how = i18n.tr(lang, "web.modinfo.telegram_how_file")
            self.assertIn("hub-telegram token", how)
            self.assertNotIn("telegram.json", how)
            self.assertNotIn("`", how)   # the module card is plain text, backticks show as is

    def test_the_token_command_asks_without_echo_in_a_terminal_and_reads_a_pipe(self):
        from unittest import mock
        seen = []
        for tty, ask in ((True, "typed:token"), (False, None)):
            with mock.patch.object(bot.Api, "call", lambda _s, m, **_: {"username": "b"}), \
                    mock.patch.object(bot, "save_config", lambda cfg: seen.append(cfg["token"])), \
                    mock.patch.object(bot, "load_config", lambda: {}), \
                    mock.patch("sys.stdin") as stdin, mock.patch("sys.stdout"), \
                    mock.patch("getpass.getpass", return_value=ask) as gp:
                stdin.isatty.return_value, stdin.readline.return_value = tty, "piped:token\n"
                bot.main(["token"])
                self.assertEqual(gp.called, tty)
        self.assertEqual(seen, ["typed:token", "piped:token"])


if __name__ == "__main__":
    unittest.main()
