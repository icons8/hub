"""Telegram, audit cases: a truncated permission card says so, the chat is checked and not only the sender,
names in the cards' HTML are escaped."""
import tests  # noqa: F401 — do not see the machine's settings
import unittest

from tests import test_telegram as base

OWNER = base.OWNER


class Case(unittest.TestCase):
    # helpers by copy, not inheritance: a TelegramTest subclass would run all of its tests again
    T = base.TelegramTest
    setUp, tearDown, spooled, msg = T.setUp, T.tearDown, T.spooled, T.msg


class Truncation(Case):
    def test_a_long_permission_input_says_it_was_cut(self):
        tail = "rm -rf /important"
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "Bash", "interactive": True,
                                                      "input": {"command": "echo ok " + "a" * 900 + tail}})
        self.bot.tick()
        text = self.fake.sent()[-1]["text"]
        self.assertNotIn("important", text)
        self.assertRegex(text, r"…\(\+\d+ characters, in full — on the page\)")

    def test_a_short_input_is_shown_whole_and_unmarked(self):
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "Bash", "interactive": True,
                                                      "input": {"command": "ls"}})
        self.bot.tick()
        text = self.fake.sent()[-1]["text"]
        self.assertIn("ls", text)
        self.assertNotIn("characters", text)


class ChatChecks(Case):
    def test_the_owner_in_a_group_is_not_the_owner(self):
        m = self.msg("do it", 5)
        m["message"]["chat"] = {"id": -100123, "type": "supergroup"}
        self.fake.updates = [m]
        self.bot.poll(timeout=0)
        self.assertEqual((self.spooled(), self.fake.sent()), ([], []))

    def test_a_private_chat_with_another_id_is_refused(self):
        m = self.msg("do it", 5)
        m["message"]["chat"] = {"id": 4242, "type": "private"}
        self.fake.updates = [m]
        self.bot.poll(timeout=0)
        self.assertEqual(self.spooled(), [])

    def test_a_chat_without_a_type_is_refused(self):
        m = self.msg("do it", 5)
        del m["message"]["chat"]["type"]
        self.fake.updates = [m]
        self.bot.poll(timeout=0)
        self.assertEqual(self.spooled(), [])

    def test_the_lost_message_path_checks_the_chat_too(self):
        m = self.msg("do it", 5)["message"]
        m["chat"] = {"id": -100123, "type": "group"}
        self.bot.lost(m, ValueError("boom"))
        self.assertEqual(self.spooled(), [])
        m["chat"] = {"id": OWNER, "type": "private"}
        self.bot.lost(m, ValueError("boom"))
        self.assertEqual(len(self.spooled()), 1)

    def test_a_button_pressed_in_another_chat_does_nothing(self):
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "Bash", "interactive": True,
                                                      "input": {"command": "ls"}})
        self.bot.tick()
        card = self.fake.sent()[-1]
        data = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.fake.updates = [{"update_id": 30, "callback_query": {
            "id": "c", "from": {"id": OWNER}, "data": data,
            "message": {"message_id": 1, "chat": {"id": -100123, "type": "group"}}}}]
        self.bot.poll(timeout=0)
        self.assertEqual(self.spooled(), [])
        self.fake.updates = [{"update_id": 31, "callback_query": {
            "id": "c", "from": {"id": OWNER}, "data": data,
            "message": {"message_id": 1, "chat": {"id": OWNER, "type": "private"}}}}]
        self.bot.poll(timeout=0)
        self.assertEqual(self.spooled()[0]["type"], "permission.answered")


class Escaping(Case):
    def test_the_tool_name_in_a_permission_card_is_escaped(self):
        self.j.append("permission.requested", "hub", {"request_id": "r1", "tool": "<b>Bash</b>&", "interactive": True,
                                                      "input": {"command": "ls"}})
        self.bot.tick()
        text = self.fake.sent()[-1]["text"]
        self.assertIn("&lt;b&gt;Bash&lt;/b&gt;&amp;", text)
        self.assertNotIn("<b>Bash</b>", text)

    def test_the_tool_card_escapes_the_tool_and_the_session(self):
        self.j.append("catalog.loaded", None, {"tools": {"x<i>y": {"kind": "mcp", "risk": ["owner-data"], "server": {}}}})
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("tools.granted", "w", {"tool": "x<i>y", "channel": "session", "owner": False, "sender": "hub"})
        self.bot.tick()
        text = self.fake.sent()[-1]["text"]
        self.assertIn("x&lt;i&gt;y", text)
        self.assertNotIn("x<i>y", text)

    def test_a_session_name_in_a_question_head_is_escaped(self):
        self.bot.on_ask("a<u>b", {"request_id": "r", "tool": "AskUserQuestion",
                                  "input": {"questions": [{"question": "Q?", "options": [{"label": "A"}]}]}})
        text = self.fake.sent()[-1]["text"]
        self.assertIn("a&lt;u&gt;b", text)
        self.assertNotIn("a<u>b", text)


if __name__ == "__main__":
    unittest.main()
