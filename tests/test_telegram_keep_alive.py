"""Bot API: one connection per thread. Every call on a new connection means TCP, TLS to the proxy, CONNECT and TLS to
Telegram all over again, while getUpdates runs every ~2 s and button-press replies follow right after it. The
connection comes from the same _connection - the same proxy as without reuse. If the server or proxy closed an idle
connection - one retry on a new one; one idle longer than KEEP_IDLE is not taken at all - the proxy may have dropped
it silently."""
import tests  # noqa: F401 - ignore the machine's settings
import socket
import unittest
from unittest import mock

from modules.telegram import bot as tg
from tests.test_telegram import FakeBotApi


class KeepAlive(unittest.TestCase):
    def setUp(self):
        self.fake = FakeBotApi()
        self.addCleanup(self.fake.server.server_close)
        self.addCleanup(self.fake.server.shutdown)
        self.fake.server.RequestHandlerClass.protocol_version = "HTTP/1.1"   # like Telegram: the connection stays open
        self.addCleanup(setattr, self.fake.server.RequestHandlerClass, "protocol_version", "HTTP/1.0")
        self.api = tg.Api("1:x", base=self.fake.base)
        self.opened = []
        real = tg._connection

        def counted(*a):
            self.opened.append(a)
            return real(*a)
        patcher = mock.patch.object(tg, "_connection", counted)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: getattr(self.api.local, "conn", None) and self.api.local.conn.close())

    def test_calls_share_one_connection_made_by_the_proxy_aware_factory(self):
        for _ in range(3):
            self.api.call("getMe")
        self.api.call("sendMessage", chat_id=1, text="x")
        self.assertEqual(len(self.opened), 1)
        self.assertEqual(len(self.fake.sent("getMe")), 3)

    def test_a_connection_closed_while_idle_is_reopened_once(self):
        self.api.call("getMe")
        self.api.local.conn.sock.shutdown(socket.SHUT_RDWR)   # the proxy or Telegram closed the idle connection
        self.assertEqual(self.api.call("sendMessage", chat_id=1, text="x"), {"message_id": 101})
        self.assertEqual(len(self.opened), 2)
        self.assertEqual(len(self.fake.sent()), 1)                     # not sent twice

    def test_a_server_that_closes_after_each_answer_gets_a_new_connection_each_time(self):
        self.fake.server.RequestHandlerClass.protocol_version = "HTTP/1.0"
        for _ in range(2):
            self.api.call("getMe")
        self.assertEqual(len(self.opened), 2)

    def test_a_fresh_connection_that_fails_is_a_transient_error(self):
        self.fake.fail["getMe"] = ["drop"]
        with self.assertRaises(tg.TgError) as e:
            self.api.call("getMe")
        self.assertTrue(e.exception.transient)
        self.assertEqual(self.api.call("getMe"), {"username": "test_bot"})

    def test_a_connection_idle_longer_than_the_limit_is_not_reused(self):
        clock = [1000.0]
        self.api.clock = lambda: clock[0]
        self.api.call("getMe")
        clock[0] += tg.KEEP_IDLE - 1
        self.api.call("getMe")
        self.assertEqual(len(self.opened), 1)
        old = self.api.local.conn
        clock[0] += tg.KEEP_IDLE + 1               # the proxy may have dropped it silently - the call would hang until the timeout
        self.api.call("getMe")
        self.assertEqual(len(self.opened), 2)
        self.assertIsNone(old.sock)                # the old one is closed, not left open
        self.assertEqual(len(self.fake.sent("getMe")), 3)

    def test_download_uses_the_same_connection(self):
        self.assertEqual(self.api.download("f1"), b"JPEGDATA")
        self.assertEqual(len(self.opened), 1)


if __name__ == "__main__":
    unittest.main()
