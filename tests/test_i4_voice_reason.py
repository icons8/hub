"""The reason a voice message was not transcribed, as the Telegram bot says it: the owner's words from the failure code
(the catalog, as on the page), not the English text of the exception, and no doubled full stop in the line to him."""
import unittest
from unittest import mock

from core import i18n
from modules.telegram import bot
from modules.voice import transcribe


class Api:
    def download(self, file_id):
        return b"raw"


def reason(error):
    b = bot.Bot.__new__(bot.Bot)
    b.api = Api()
    media = {"file_id": "f", "mime_type": "audio/ogg"}
    with mock.patch.object(transcribe, "transcribe_retry", side_effect=error):
        return b.transcribe(media, {})


class VoiceReason(unittest.TestCase):
    def test_a_failure_code_is_said_in_the_catalog_words(self):
        text, why = reason(transcribe._fail("ffmpeg: boom", "ffmpeg_failed"))
        self.assertIsNone(text)
        self.assertEqual(why, i18n.t("web.srv.voice_why_ffmpeg_failed"))

    def test_an_unknown_code_is_the_general_reason(self):
        self.assertEqual(reason(transcribe.VoiceError("x"))[1], i18n.t("web.srv.voice_why_other"))

    def test_no_speech_has_no_full_stop_of_its_own(self):
        why = reason(transcribe.Empty("empty transcription"))[1]
        self.assertFalse(why.endswith("."))
        self.assertEqual(why + ".", i18n.t("web.srv.no_speech"))

    def test_the_bot_knows_every_code_the_page_does(self):
        from modules.web import server
        self.assertEqual(bot.VOICE_WHY, server.Web.VOICE_WHY)


if __name__ == "__main__":
    unittest.main()
