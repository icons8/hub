"""The Voice module (owner 30.09: voice recognition by an external command, VOICE_CMD, as a module too: once
configured, the web gets a microphone and Telegram gets recognition): audio -> ffmpeg -> 16 kHz WAV -> the command
from the settings ({wav}) -> text from the last JSON line of its output. Not configured means no module."""
import tests  # noqa: F401 — ignore the machine's settings
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from modules.voice import transcribe as voice
from platforms import current
from tests.fakes.exe import script


class VoiceTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="voice-")
        # a fake ffmpeg: copies the input to the output (the last argument) so the command gets a file
        self.ffmpeg = script(os.path.join(self.dir, "ffmpeg"),
                             "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        self.asr = script(os.path.join(self.dir, "asr"),
                          "import sys, json\nwav = sys.argv[sys.argv.index('--file') + 1]\n"
                          "data = open(wav, 'rb').read().decode()\n"
                          "print('loading model…')\nprint(json.dumps({'text': '  hello, ' + data + ' '}))\n")
        self.cfg = {"voice": {"cmd": f"{self.asr} --file {{wav}} --json", "ffmpeg": self.ffmpeg}}

    def test_enabled_only_when_configured(self):
        self.assertTrue(voice.enabled(self.cfg))
        self.assertFalse(voice.enabled({}))
        self.assertFalse(voice.enabled({"voice": {"cmd": ""}}))

    def test_sniff_names_the_container_by_its_first_bytes(self):
        self.assertEqual(voice.sniff(b"\0\0\0\x1cftypM4A \0\0\0\0"), ".m4a")
        self.assertEqual(voice.sniff(b"OggS\0\2"), ".ogg")
        self.assertEqual(voice.sniff(b"\x1a\x45\xdf\xa3\x01"), ".webm")
        self.assertEqual(voice.sniff(b"RIFF\0\0\0\0WAVE"), ".wav")
        self.assertIsNone(voice.sniff(b"#EXTM3U\nhttp://x/"))   # a playlist and the like are not accepted
        self.assertIsNone(voice.sniff(b"rec"))

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
    def test_a_safari_recording_is_read(self):
        """Safari: MediaRecorder produces fragmented mp4 with AAC (audio/mp4;codecs=mp4a.40.2), which a real ffmpeg reads."""
        src = os.path.join(self.dir, "safari.mp4")
        subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=duration=1", "-c:a", "aac",
                        "-movflags", "frag_keyframe+empty_moov", "-f", "mp4", src], check=True)
        asr = script(os.path.join(self.dir, "wavsize"),
                     "import sys, json, os\nprint(json.dumps({'text': 'size %d' % (os.path.getsize(sys.argv[1]) > 30000)}))\n")
        cfg = {"voice": {"cmd": f"{asr} {{wav}}"}}
        raw = open(src, "rb").read()
        self.assertEqual(voice.sniff(raw), ".m4a")   # the real header is recognised, and ffmpeg reads by this suffix
        self.assertEqual(voice.suffix_of_mime("audio/mp4;codecs=mp4a.40.2"), ".m4a")
        self.assertEqual(voice.transcribe(raw, cfg, suffix=voice.sniff(raw)), "size 1")

    def test_transcribe_bytes(self):
        self.assertEqual(voice.transcribe(b"mir", self.cfg, suffix=".ogg"), "hello, mir")

    def test_command_failure_and_empty_text(self):
        bad = script(os.path.join(self.dir, "bad"), "import sys\nprint('error: no login', file=sys.stderr)\nsys.exit(3)\n")
        with self.assertRaises(voice.VoiceError) as e:
            voice.transcribe(b"x", {"voice": {"cmd": f"{bad} {{wav}}", "ffmpeg": self.ffmpeg}})
        self.assertIn("no login", str(e.exception))
        empty = script(os.path.join(self.dir, "empty"), "print('{\"text\": \"  \"}')\n")
        with self.assertRaises(voice.VoiceError):
            voice.transcribe(b"x", {"voice": {"cmd": f"{empty} {{wav}}", "ffmpeg": self.ffmpeg}})

    def test_partial_text_of_a_failed_command_is_kept(self):
        """The command crashed but managed to transcribe part: {"partial": …} in its output. The error carries it (the
        client shows the owner what was heard). Without it, empty."""
        cut = script(os.path.join(self.dir, "cut"), "import sys, json\nprint(json.dumps({'partial': ' buy bread '}))\n"
                                                     "sys.exit('looped')\n")
        with self.assertRaises(voice.VoiceError) as e:
            voice.transcribe(b"x", {"voice": {"cmd": f"{cut} {{wav}}", "ffmpeg": self.ffmpeg}})
        self.assertEqual(e.exception.partial, "buy bread")
        bad = script(os.path.join(self.dir, "bad2"), "import sys\nprint('{not json')\nsys.exit(3)\n")
        with self.assertRaises(voice.VoiceError) as e:
            voice.transcribe(b"x", {"voice": {"cmd": f"{bad} {{wav}}", "ffmpeg": self.ffmpeg}})
        self.assertEqual(e.exception.partial, "")

    def test_failure_carries_a_short_kind_without_paths(self):
        """The error carries a reason code (kind): the page uses it to tell the owner what broke without showing paths or output."""
        def kind(cfg, audio=b"x", suffix=".ogg"):
            with self.assertRaises(voice.VoiceError) as e:
                voice.transcribe(audio, cfg, suffix=suffix)
            return e.exception.kind
        asr = f"{script(os.path.join(self.dir, 'ok'), 'print(1)')} {{wav}}"
        self.assertEqual(kind({"voice": {"cmd": asr, "ffmpeg": self.ffmpeg}}, suffix=".xyz"), "format")
        self.assertEqual(kind({"voice": {"cmd": asr, "ffmpeg": "/none/ffmpeg"}}), "ffmpeg_start")
        nowav = script(os.path.join(self.dir, "nowav"), "import sys\nsys.exit(1)\n")
        self.assertEqual(kind({"voice": {"cmd": asr, "ffmpeg": nowav}}), "ffmpeg_failed")
        self.assertEqual(kind({"voice": {"cmd": "/none/asr {wav}", "ffmpeg": self.ffmpeg}}), "engine_start")
        bad = script(os.path.join(self.dir, "bad3"), "import sys\nsys.exit(3)\n")
        self.assertEqual(kind({"voice": {"cmd": f"{bad} {{wav}}", "ffmpeg": self.ffmpeg}}), "engine_failed")
        junk = script(os.path.join(self.dir, "junk"), "print('not json')\n")
        self.assertEqual(kind({"voice": {"cmd": f"{junk} {{wav}}", "ffmpeg": self.ffmpeg}}), "engine_output")
        slow = script(os.path.join(self.dir, "slow"), "import time\ntime.sleep(60)\n")
        with mock.patch.object(voice, "TIMEOUT", 1):
            self.assertEqual(kind({"voice": {"cmd": f"{slow} {{wav}}", "ffmpeg": self.ffmpeg}}), "engine_timeout")

    def test_retries_then_succeeds(self):
        flaky = script(os.path.join(self.dir, "flaky"),
                       f"import os, sys, json\nn = os.path.join({self.dir!r}, 'n')\n"
                       "c = int(open(n).read()) if os.path.exists(n) else 0\nopen(n, 'w').write(str(c + 1))\n"
                       "sys.exit(1) if c < 2 else print(json.dumps({'text': 'ok'}))\n")
        cfg = {"voice": {"cmd": f"{flaky} {{wav}}", "ffmpeg": self.ffmpeg, "backoff": [1, 1, 1]}}
        slept = []
        self.assertEqual(voice.transcribe_retry(b"x", cfg, sleep=slept.append), "ok")
        self.assertEqual(slept, [1, 1])
        with self.assertRaises(voice.VoiceError):
            voice.transcribe_retry(b"x", {"voice": {"cmd": "/nope {wav}", "ffmpeg": self.ffmpeg, "backoff": [0]}},
                                   sleep=slept.append)

    def test_no_speech_is_not_retried(self):
        """"No speech": a retry would hear the same, so it goes out at once with no pauses (it used to be 5 attempts and ~6.7 min of pauses)."""
        n = os.path.join(self.dir, "calls")
        empty = script(os.path.join(self.dir, "empty"),
                       f"open({n!r}, 'a').write('x')\nprint('{{\"text\": \"\"}}')\n")
        slept = []
        with self.assertRaises(voice.Empty):
            voice.transcribe_retry(b"x", {"voice": {"cmd": f"{empty} {{wav}}", "ffmpeg": self.ffmpeg,
                                                    "backoff": [1, 1]}}, sleep=slept.append)
        self.assertEqual((slept, open(n).read()), ([], "x"))

    def test_timeout_kills_the_whole_command_tree(self):
        """Without a cancel (page, Telegram), on timeout the recognition command's descendant is killed too, not only the command."""
        pid = os.path.join(self.dir, "child")
        tree = script(os.path.join(self.dir, "tree"),
                      "import subprocess, sys, time\n"
                      "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
                      " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                      f"open({pid!r}, 'w').write(str(c.pid))\ntime.sleep(60)\n")
        with mock.patch.object(voice, "TIMEOUT", 2), self.assertRaises(voice.VoiceError):
            voice.transcribe(b"x", {"voice": {"cmd": f"{tree} {{wav}}", "ffmpeg": self.ffmpeg}})
        with open(pid) as f:
            child = int(f.read())
        self.addCleanup(lambda: current.alive(child) and os.kill(child, 9))   # if the kill broke, do not leave an orphan
        for _ in range(50):
            if not current.alive(child):
                break
            time.sleep(0.1)
        self.assertFalse(current.alive(child))


if __name__ == "__main__":
    unittest.main()
