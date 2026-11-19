"""Transcribing audio with the hub's provider from a module process (Module API 2.4, the voice.transcribe right):
hubmod.transcribe takes the voice section of the hub settings itself — the module gets only the result {text} or
{empty: true}, a refusal is a TranscribeError with a code (not text); a timeout and cancellation kill the started
provider call together with its processes."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import hubmod, modules
from modules.voice import transcribe as voice
from platforms import current
from tests.fakes.exe import script


class TranscribeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubmod-voice-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.mod = os.path.join(self.dir, "mod")
        os.makedirs(self.mod)
        self.manifest(["voice.transcribe"])
        self.ffmpeg = script(os.path.join(self.dir, "ffmpeg"),
                             "import sys, shutil\na = sys.argv\nshutil.copy(a[a.index('-i') + 1], a[-1])\n")
        self.asr = script(os.path.join(self.dir, "asr"),
                          "import sys, json\ndata = open(sys.argv[1], 'rb').read().decode()\n"
                          "print(json.dumps({'text': ' ' + data + ' '}))\n")
        self.config = os.path.join(self.dir, "hub.json")
        self.settings({"cmd": f"{self.asr} {{wav}}", "ffmpeg": self.ffmpeg})
        env = {"HUB_CONFIG": self.config, "HUB_STATE_DIR": self.dir, "HUB_MODULE": "talk"}
        patch = mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("VOICE_CMD", None)

    def manifest(self, caps):
        with open(os.path.join(self.mod, "module.json"), "w") as f:
            json.dump({"name": "talk", "version": "1.0.0", "api": "^2.4", "run": ["python3", "main.py"],
                       "capabilities": caps}, f)

    def settings(self, voice_section):
        with open(self.config, "w") as f:
            json.dump({"voice": voice_section, "talk": {"x": 1}}, f)

    def call(self, audio=b"mir", mime="audio/webm", **kw):
        return hubmod.transcribe(audio, mime, folder=self.mod, **kw)

    def code(self, **kw):
        with self.assertRaises(hubmod.TranscribeError) as e:
            self.call(**kw)
        return e.exception.code

    def test_text_only(self):
        self.assertEqual(self.call(), {"text": "mir"})
        self.assertEqual(self.call(mime="audio/ogg; codecs=opus"), {"text": "mir"})
        self.assertEqual(self.call(timeout=30, cancel=threading.Event()), {"text": "mir"})   # the path with cancellation

    def test_no_speech_is_empty_not_error(self):
        empty = script(os.path.join(self.dir, "empty"), "print('{\"text\": \"  \"}')\n")
        self.settings({"cmd": f"{empty} {{wav}}", "ffmpeg": self.ffmpeg})
        self.assertEqual(self.call(), {"empty": True})
        self.assertEqual(self.call(audio=b""), {"empty": True})
        # the core and the page — as before: VoiceError with the same text
        with self.assertRaises(voice.VoiceError) as e:
            voice.transcribe(b"x", {"voice": {"cmd": f"{empty} {{wav}}", "ffmpeg": self.ffmpeg}})
        self.assertIsInstance(e.exception, voice.Empty)
        self.assertEqual(str(e.exception), "empty transcription")

    def test_codes(self):
        self.assertEqual(self.code(mime="audio/aiff"), "format")
        self.assertEqual(self.code(mime=None), "format")
        bad = script(os.path.join(self.dir, "bad"), "import sys\nprint('no login', file=sys.stderr)\nsys.exit(3)\n")
        self.settings({"cmd": f"{bad} {{wav}}", "ffmpeg": self.ffmpeg})
        with self.assertRaises(hubmod.TranscribeError) as e:
            self.call()
        self.assertEqual(e.exception.code, "failed")
        self.assertIn("no login", str(e.exception))
        raw = script(os.path.join(self.dir, "raw"), "import sys\nsys.stdout.buffer.write(b'\\xff\\xfe{}\\n')\n")
        self.settings({"cmd": f"{raw} {{wav}}", "ffmpeg": self.ffmpeg})
        self.assertEqual(self.code(cancel=threading.Event()), "failed")   # output not in UTF-8 — a failure, not an exception
        self.settings({})
        self.assertEqual(self.code(), "off")
        self.assertEqual(self.code(audio=b""), "off")   # empty audio does not call the provider, but the setting is checked
        with open(self.config, "w") as f:
            f.write("{")
        self.assertEqual(self.code(), "failed")
        with open(os.path.join(self.mod, "module.json"), "w") as f:
            f.write("{")
        self.assertEqual(self.code(), "denied")

    def test_voice_available(self):
        """«Is voice available?» — without calling the provider and without audio: the module need not build this on the order of transcribe's checks."""
        called = os.path.join(self.dir, "called")
        asr = script(os.path.join(self.dir, "asr2"), f"open({called!r}, 'w').close()\nprint('{{\"text\": \"x\"}}')\n")
        self.settings({"cmd": f"{asr} {{wav}}", "ffmpeg": self.ffmpeg})
        self.assertIs(hubmod.voice_available(folder=self.mod), True)
        self.assertFalse(os.path.exists(called))
        self.settings({})
        self.assertIs(hubmod.voice_available(folder=self.mod), False)
        with open(self.config, "w") as f:
            f.write("{")
        with self.assertRaises(hubmod.TranscribeError) as e:
            hubmod.voice_available(folder=self.mod)
        self.assertEqual(e.exception.code, "failed")
        self.settings({"cmd": f"{asr} {{wav}}", "ffmpeg": self.ffmpeg})
        self.manifest(["http"])
        with self.assertRaises(hubmod.TranscribeError) as e:
            hubmod.voice_available(folder=self.mod)
        self.assertEqual(e.exception.code, "denied")

    def test_needs_capability(self):
        self.manifest(["http"])
        self.assertEqual(self.code(), "denied")
        with mock.patch.dict(os.environ, {"HUB_MODULE": ""}):   # not a module process started by the hub
            self.manifest(["voice.transcribe"])
            self.assertEqual(self.code(), "denied")
        with mock.patch.dict(os.environ, {"HUB_STATE_DIR": ""}):
            self.assertEqual(self.code(), "denied")

    def test_capability_in_manifest_since_24(self):
        man = {"name": "talk", "version": "1.0.0", "api": "^2.4", "run": ["python3", "main.py"],
               "capabilities": ["voice.transcribe"]}
        self.assertEqual(modules.validate(man), [])
        errs = modules.validate({**man, "api": "^2.3"})
        self.assertTrue(any("voice.transcribe" in e and "2.4" in e for e in errs), errs)

    def slow(self):
        pid = os.path.join(self.dir, "pid")
        slow = script(os.path.join(self.dir, "slow"),
                      f"import os, time\nopen({pid!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
        self.settings({"cmd": f"{slow} {{wav}}", "ffmpeg": self.ffmpeg})
        return pid

    def gone(self, pidfile):
        with open(pidfile) as f:
            pid = int(f.read())
        for _ in range(50):
            if not current.alive(pid):
                return True
            time.sleep(0.1)
        return False

    def test_timeout_kills_provider(self):
        pid = self.slow()
        t0 = time.monotonic()
        self.assertEqual(self.code(timeout=4), "timeout")
        self.assertLess(time.monotonic() - t0, 10)
        self.assertTrue(self.gone(pid))

    def test_cancel_kills_provider(self):
        pid = self.slow()
        stop = threading.Event()

        def later():
            while not os.path.exists(pid):
                time.sleep(0.05)
            stop.set()
        threading.Thread(target=later, daemon=True).start()
        t0 = time.monotonic()
        self.assertEqual(self.code(cancel=stop), "cancelled")
        self.assertLess(time.monotonic() - t0, 10)
        self.assertTrue(self.gone(pid))
        self.assertEqual(self.code(cancel=stop), "cancelled")   # cancelled in advance does not call the provider


if __name__ == "__main__":
    unittest.main()
