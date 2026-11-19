"""`hub bus relay join`: the invitation is a secret, so it comes on stdin or from a file like `hub bus join -` (an argument
shows in ps and the shell history); and a refusal of an invitation says what was wrong with it — a slip of the paste is
not «intercepted»."""
import tests  # noqa: F401 — keeps the machine's settings out
import contextlib
import io
import os
import re
import tempfile
import unittest
from unittest import mock

from core import busflow, buscli, busrelay, i18n

LINE = "hub-relay-invite-1:" + "A" * 40 + "." + "B" * 20
STATE = {"bus": {"enabled": True, "me": {"fp": "SHA256:x"}}}


def run(argv, stdin=""):
    """(code, stdout, the invitation text that reached Flow.relay_join or None)."""
    got = []

    def join(self, text, b=None, keys=None):
        got.append(text)
        return busflow.Done(url="https://relay.example.test", by="operator")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO(stdin)), \
            mock.patch.object(busflow.Flow, "relay_join", join), mock.patch.object(buscli.Cli, "_keys", lambda s, b: None):
        code = buscli.main(argv, lambda *a, **k: 1, lambda: STATE, tempfile.gettempdir())
    return code, out.getvalue(), got[0] if got else None


class RelayJoinInput(unittest.TestCase):
    def test_a_dash_is_stdin_not_the_invitation(self):
        code, out, text = run(["relay", "join", "-"], stdin=LINE + "\n")
        self.assertEqual((code, text), (0, LINE))

    def test_a_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "invite.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("here: " + LINE + "\n")
            code, _, text = run(["relay", "join", "--file", path])
        self.assertEqual((code, text), (0, LINE))

    def test_file_dash_is_stdin_too(self):
        self.assertEqual(run(["relay", "join", "--file", "-"], stdin=LINE)[2], LINE)

    def test_the_argument_still_works(self):
        self.assertEqual(run(["relay", "join", LINE])[2], LINE)

    def test_an_empty_stdin_or_a_missing_file_is_a_refusal_not_a_join(self):
        for argv, stdin in ((["relay", "join", "-"], "  \n"), (["relay", "join", "--file", "/nonexistent/x"], "")):
            code, out, text = run(argv, stdin)
            self.assertEqual((code, text), (1, None), out)
            self.assertTrue(out.strip())

    def test_a_dash_with_more_words_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            run(["relay", "join", "-", "x"], stdin=LINE)

    def test_a_file_flag_without_one_path_is_a_usage_error_not_a_traceback(self):
        for argv in (["relay", "join", "--file"], ["relay", "join", "--file", "a", "b"]):
            with self.assertRaises(SystemExit) as e:
                run(argv, stdin=LINE)
            self.assertIn("--file", str(e.exception))

    def test_stdin_that_is_not_text_is_a_refusal(self):
        class Bad(io.StringIO):
            def read(self, *a):
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", Bad()), \
                mock.patch.object(buscli.Cli, "_keys", lambda s, b: None):
            code = buscli.main(["relay", "join", "-"], lambda *a, **k: 1, lambda: STATE, tempfile.gettempdir())
        self.assertEqual(code, 1)
        self.assertIn(i18n.t("cli.text.stdin"), out.getvalue())


class EachRefusalHasItsWords(unittest.TestCase):
    REASONS = ("not an invitation", "base64url", "the invitation's fields", "the relay's key",
               "the invitation is for another relay", "no such invitation", "the invitation was withdrawn",
               "the invitation was used", "the invitation expired", "the hub that made the invitation is not here",
               "the invitation's signature does not check")

    def test_only_a_used_one_says_intercepted(self):
        for reason in self.REASONS:
            text = busflow.why("bad_invite", reason)
            self.assertEqual("intercepted" in text, reason == "the invitation was used", (reason, text))

    def test_a_slip_of_the_paste_says_so(self):
        for reason in ("not an invitation", "base64url", "the invitation's fields", "the relay's key"):
            text = busflow.why("bad_invite", reason)
            self.assertIn("hub-relay-invite-1:", text, reason)
            self.assertNotIn("used", text)

    def test_the_reasons_differ(self):
        texts = {busflow.why("bad_invite", r) for r in self.REASONS}
        self.assertGreaterEqual(len(texts), 7, texts)
        self.assertEqual(busflow.why("bad_invite", "the invitation expired"), busflow.why("expired"))
        self.assertEqual(busflow.why("bad_invite", "the invitation was used"), busflow.why("used"))

    def test_every_reason_the_relay_gives_has_its_own_words(self):
        # the relay's prose is the key: a reworded refusal there must not fall into the plain one unseen
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "relay", "server.py"), encoding="utf-8") as f:
            sent = set(re.findall(r'Refused\(\d+, "bad_invite", "([^"]+)"', f.read()))
        self.assertIn("the invitation was used", sent)
        plain = busflow.why("bad_invite", "")
        for reason in sent:
            if reason.startswith(("the invitation must name", "the invitation is not signed")):
                continue   # the check of an invitation a hub makes (hub bus relay invite), not of a join
            self.assertNotEqual(busflow.why("bad_invite", reason), plain, reason)

    def test_an_unknown_reason_is_neutral(self):
        for reason in ("", "something new"):
            self.assertNotIn("intercepted", busflow.why("bad_invite", reason))

    def test_the_local_parse_of_a_junk_line_through_the_cli(self):
        try:
            busrelay.read_invite("hello")
        except busrelay.Error as e:
            self.assertIn("hub-relay-invite-1:", busflow.why("bad_invite", str(e)))
        self.assertIn("hub-relay-invite-1:", i18n.t("bus.flow.invite.form"))

    def test_the_words_follow_the_language(self):
        ru = busflow.why("bad_invite", "not an invitation", lang="ru")
        self.assertNotEqual(ru, busflow.why("bad_invite", "not an invitation", lang="en"))


if __name__ == "__main__":
    unittest.main()
