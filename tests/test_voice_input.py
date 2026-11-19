"""Voice: ffmpeg gets the input format explicitly and reads only files (audio from outside cannot pass as a playlist or a
network stream); the suffix goes by what Telegram says about the file."""
import tests  # noqa: F401 — hide the machine's settings
import io
import os
import shutil
import tempfile
import unittest
import wave
from unittest import mock

from modules.voice import transcribe as voice
from tests import test_telegram as tg_base
from tests.fakes.exe import script


class FfmpegInput(unittest.TestCase):
    def run_with(self, suffix):
        """The argv that transcription gives ffmpeg (ffmpeg itself is a stub, recognition is a real script)."""
        calls = []
        asr = script(os.path.join(tempfile.mkdtemp(prefix="ff-"), "asr"), "import json\nprint(json.dumps({'text': 'ok'}))\n")
        cfg = {"voice": {"cmd": f"{asr} {{wav}}", "ffmpeg": "ffmpeg"}}
        real = voice._run

        def route(argv, *a, **kw):
            if argv[0] != "ffmpeg":
                return real(argv, *a, **kw)
            calls.append(argv)
            open(argv[-1], "wb").close()
            return mock.Mock(returncode=0, stderr=b"")
        with mock.patch.object(voice, "_run", side_effect=route):
            voice.transcribe(b"x", cfg, suffix=suffix)
        return calls[0]

    def test_the_input_format_is_explicit_and_only_files_are_read(self):
        argv = self.run_with(".ogg")
        i = argv.index("-i")
        self.assertEqual(argv[argv.index("-f") + 1], "ogg")
        self.assertLess(argv.index("-f"), i)                    # the input parameter comes before -i
        self.assertEqual(argv[argv.index("-protocol_whitelist") + 1], "file")
        self.assertLess(argv.index("-protocol_whitelist"), i)

    def test_mp4_video_notes_get_the_mp4_demuxer(self):
        argv = self.run_with(".mp4")
        self.assertEqual(argv[argv.index("-f") + 1], "mp4")

    def test_an_unknown_suffix_is_refused_not_guessed(self):
        with self.assertRaises(voice.VoiceError):
            voice.transcribe(b"x", {"voice": {"cmd": "asr {wav}", "ffmpeg": "ffmpeg"}}, suffix=".txt")

    def test_the_suffix_follows_what_telegram_says_about_the_audio(self):
        self.assertEqual(voice.suffix_for({"file_id": "f", "duration": 3}), ".ogg")                    # a voice message
        self.assertEqual(voice.suffix_for({"file_id": "f", "length": 240}), ".mp4")                    # a video note
        self.assertEqual(voice.suffix_for({"file_id": "f", "mime_type": "audio/mpeg"}), ".mp3")
        self.assertEqual(voice.suffix_for({"file_id": "f", "mime_type": "audio/mp4"}), ".m4a")
        self.assertEqual(voice.suffix_for({"file_id": "f", "mime_type": "audio/ogg"}), ".ogg")
        self.assertIsNone(voice.suffix_for({"file_id": "f", "mime_type": "application/x-concat"}))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_real_ffmpeg_trusts_the_suffix_not_the_content(self):
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\0\0" * 1600)
        with self.assertRaises(voice.VoiceError):       # WAV passed off as Ogg is not decoded as WAV
            voice.transcribe(buf.getvalue(), {"voice": {"cmd": "true {wav}"}}, suffix=".ogg")



class TelegramAudio(unittest.TestCase):
    T = tg_base.TelegramTest
    setUp, tearDown, msg = T.setUp, T.tearDown, T.msg

    def test_an_audio_of_an_unknown_type_is_refused_in_words(self):
        # a type that transcription does not take: a refusal in words, the file is not even downloaded
        self.bot.hub_config = lambda: {"voice": {"cmd": "asr {wav}", "ffmpeg": "/nope/ffmpeg", "backoff": [0]}}
        self.bot.voice_sync = True
        self.fake.updates = [self.msg("", 5, audio={"file_id": "f1", "mime_type": "application/x-concat"})]
        self.bot.poll(timeout=0)
        self.bot.drain_voice()
        self.assertEqual(self.fake.sent("getFile"), [])
        self.assertTrue(any("is not supported" in b["text"] for b in self.fake.sent()), self.fake.sent())


if __name__ == "__main__":
    unittest.main()
