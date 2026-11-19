"""Hub text is UTF-8 regardless of the locale: Russian Windows (cp1251), KOI8-R on Linux (05.10)."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

from core import catalog
from tests.fakes import lang

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(REPO, "hooks", "hub_hook.py")
# a Russian word of the language data whose capital first letter has byte 0x98 in UTF-8: cp1251 has no letter there
WORD = lang.word("words", "topic_label", 1).capitalize()


class HookStdinTest(unittest.TestCase):
    def test_marker_and_non_ascii_survive_a_non_utf8_locale(self):
        # Claude Code writes UTF-8 to the hook; the hook read stdin in the locale encoding: Cyrillic text turned into
        # mojibake and the marker was not recognized, while a letter whose UTF-8 byte is 0x98 in cp1251, or an emoji,
        # crashed the hook — turn.started was lost
        state = tempfile.mkdtemp(prefix="hook-enc-")
        self.assertIn(b"\x98", WORD[0].encode("utf-8"))
        prompt = f"{lang.head('m3', kind='owner')} {WORD} 😀"
        env = {**os.environ, "HUB_SESSION": "a", "HUB_STATE_DIR": state, "PYTHONIOENCODING": "cp1251",
               "PYTHONUTF8": "0"}
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s", "prompt": prompt},
                             ensure_ascii=False).encode("utf-8")
        out = subprocess.run([sys.executable, HOOK], input=payload, env=env, capture_output=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        spool = os.path.join(state, "spool")
        facts = []
        for name in sorted(os.listdir(spool)):
            with open(os.path.join(spool, name), encoding="utf-8") as f:
                facts.append(json.load(f))
        self.assertEqual([(f["type"], f["data"]["marker"], f["data"]["prompt_head"]) for f in facts],
                         [("turn.started", "m3", prompt)])
        self.assertFalse(os.path.exists(os.path.join(state, "hook-errors.log")))

    def test_a_lone_surrogate_in_the_prompt_does_not_lose_the_fact(self):
        # Claude Code's JSON can carry a lone surrogate as an escape ("\\udc80": a byte that was not UTF-8 in the
        # owner's text); json.loads keeps it, and writing the spool failed with UnicodeEncodeError — turn.started was
        # lost. It becomes U+FFFD, the marker and the words stay
        state = tempfile.mkdtemp(prefix="hook-enc-")
        head = lang.head("m4", kind="owner")
        payload = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "s",
                              "prompt": f"{head} came through: \udc80 check"}).encode("ascii")
        self.assertIn(b"\\udc80", payload)
        env = {**os.environ, "HUB_SESSION": "a", "HUB_STATE_DIR": state}
        out = subprocess.run([sys.executable, HOOK], input=payload, env=env, capture_output=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        spool = os.path.join(state, "spool")
        facts = []
        for name in sorted(os.listdir(spool)):
            with open(os.path.join(spool, name), encoding="utf-8") as f:
                facts.append(json.load(f))
        self.assertEqual([(f["type"], f["data"]["marker"], f["data"]["prompt_head"]) for f in facts],
                         [("turn.started", "m4", f"{head} came through: \ufffd check")])


class ConfigEncodingTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(prefix="cfg-enc-"), "hub.json")
        self.prev = os.environ.get("HUB_CONFIG")
        os.environ["HUB_CONFIG"] = self.path

    def tearDown(self):
        if self.prev is None:  # it was not set — it is not set afterwards (os.environ does not accept None)
            os.environ.pop("HUB_CONFIG", None)
        else:
            os.environ["HUB_CONFIG"] = self.prev

    def write(self, data, encoding):
        with open(self.path, "w", encoding=encoding) as f:
            json.dump(data, f, ensure_ascii=False)

    def test_utf8_with_and_without_bom(self):
        # hub.json is edited by a person: a Cyrillic letter in a project description crashed hubd and the page
        # (UnicodeDecodeError, byte 0x98), and Windows PowerShell 5.1 with -Encoding utf8 writes a BOM that json chokes on
        about = f"{WORD} {WORD.lower()}"
        for encoding in ("utf-8", "utf-8-sig"):
            self.write({"projects": {"p": {"path": "/tmp", "about": about}}}, encoding)
            self.assertEqual(catalog.load_config("/nonexistent")["projects"]["p"]["about"], about, encoding)

    def test_settings_written_in_the_locale_by_older_code_are_read(self):
        # review of PR #18: a hub.json written by the earlier code under KOI8-R crashed hubd after the move to UTF-8
        from unittest import mock
        self.write({"projects": {"p": {"path": "/tmp", "about": WORD}}}, "koi8-r")
        # hub processes run in UTF-8 mode (on Windows always): the "preferred" encoding there is utf-8, the locale's own differs
        with (mock.patch("core.catalog.locale.getpreferredencoding", return_value="utf-8"),
              mock.patch("core.catalog.locale.getencoding", return_value="koi8-r")):
            self.assertEqual(catalog.load_config("/nonexistent")["projects"]["p"]["about"], WORD)

    def test_utf16_is_refused_with_a_clear_error(self):
        # `Out-File` and `>` in PowerShell 5.1 write UTF-16 — a clear error instead of UnicodeDecodeError
        self.write({"timezone": "UTC"}, "utf-16")
        with self.assertRaisesRegex(ValueError, "not in UTF-8"):
            catalog.load_config("/nonexistent")


if __name__ == "__main__":
    unittest.main()
