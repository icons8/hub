"""Settings → Modules → Voice: the command used for transcription is shown read-only (owner decision 2026-10-03: show
the command without the ability to edit it); anything key-like in the command is hidden."""
import tests  # noqa: F401 — do not see the machine's settings
import tempfile
import unittest

from modules.web import modinfo


class VoiceCard(unittest.TestCase):
    def card(self, cfg):
        mods = modinfo.modules(tempfile.mkdtemp(), cfg, {"sessions": []})
        return next(m for m in mods if m["id"] == "voice")

    def test_command_is_shown_when_set(self):
        cmd = "/opt/tools/stt --transcribe-file {wav} --json"
        self.assertEqual(self.card({"voice": {"cmd": cmd}})["command"], cmd)
        self.assertNotIn("command", self.card({}))

    def test_key_like_parts_are_hidden(self):
        cmd = "/opt/tools/stt --key sk_live_ab12CD34ef56GH78ij90KL12 --model /models/large-v3-turbo.bin {wav}"
        shown = modinfo.masked(cmd)
        self.assertNotIn("ab12CD34", shown)
        self.assertIn("/models/large-v3-turbo.bin", shown)              # paths are left alone
        self.assertIn("•••", shown)


if __name__ == "__main__":
    unittest.main()
