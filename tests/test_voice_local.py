"""The built-in voice (spec d2109189): a model the hub downloads itself, run per recording like the recognition command.

A fake transcribe_cpp package stands in for the native engine: the wrapper is what local.py imports from the installed
py/ folder, so the test lays out the same files the setup step does. The fake speech detector marks every run of
non-zero samples as speech; the fake model writes how many samples it got (and leaves a mark when it is loaded; with
FAKE_ASR_TAG, its file name first; FAKE_ASR_LANGS — the languages it knows, another one is refused like the engine
refuses it; the language it was given is written to FAKE_ASR_LANG_MARK)."""
import tests  # noqa: F401 — ignore the machine's settings
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import collections
import tempfile
import threading
import unittest
import wave
import zipfile
from unittest import mock

import platforms
from modules.voice import install, local
from modules.voice import transcribe as voice
from tests.fakes.exe import script

FAKE = '''
import array, os
LOADED = os.environ.get("FAKE_ASR_MARK")


class _Seg:
    def __init__(self, a, b):
        self.start_sample, self.end_sample = a, b


class _Vad:
    def run(self, x):
        segs, start = [], None
        for i, v in enumerate(list(x) + [0.0]):
            if v and start is None:
                start = i
            elif not v and start is not None:
                segs.append(_Seg(start, i))
                start = None
        return type("R", (), {"segments": segs})()


class _Session:
    def __init__(self, path):
        self.path = path

    def run(self, x, language=None):
        if os.environ.get("FAKE_ASR_FAIL"):
            raise RuntimeError("GGML_ASSERT")
        if language and os.environ.get("FAKE_ASR_LANGS") and language not in os.environ["FAKE_ASR_LANGS"].split(","):
            raise RuntimeError("transcribe_run: unsupported language (status 10)")
        if os.environ.get("FAKE_ASR_LANG_MARK"):
            open(os.environ["FAKE_ASR_LANG_MARK"], "a").write("%s\\n" % language)
        tag = os.path.basename(self.path) + ":" if os.environ.get("FAKE_ASR_TAG") else ""
        return type("R", (), {"text": " %sw%d " % (tag, len(x))})()


class Model:
    def __init__(self, path, backend="cpu"):
        assert os.path.isfile(path), path
        self.path = path

    def vad_session(self):
        return _Vad()

    def session(self, n_threads=1):
        if LOADED:
            open(LOADED, "a").write("loaded\\n")
        return _Session(self.path)
'''


def lay_out(state, model_size=None, model="gigaam-v3-e2e-rnnt", file="m.gguf", language=None, name="GigaAM v3"):
    """The files of an installed built-in voice, as the setup step leaves them, with the fake wrapper."""
    base = local.runtime(state)
    py = os.path.join(base, "voice", "0.4.1", "py", "transcribe_cpp")
    os.makedirs(py, exist_ok=True)
    with open(os.path.join(py, "__init__.py"), "w") as f:
        f.write(FAKE)
    native = os.path.join(base, "voice", "0.4.1", "native")
    os.makedirs(native, exist_ok=True)
    open(os.path.join(native, "libtranscribe.so"), "wb").write(b"lib")
    models = os.path.join(base, "voice-models")
    os.makedirs(models, exist_ok=True)
    open(os.path.join(models, file), "wb").write(b"model!")
    open(os.path.join(models, "v.gguf"), "wb").write(b"vad")
    rec = {"engine": "0.4.1", "model": model, "name": name, "lang": "ru",
           "files": {"lib": {"path": "voice/0.4.1/native/libtranscribe.so", "size": 3},
                     "py": {"path": "voice/0.4.1/py"},
                     "model": {"path": "voice-models/" + file, "size": model_size or 6},
                     "vad": {"path": "voice-models/v.gguf", "size": 3}}}
    if language:
        rec["language"] = language
    with open(os.path.join(base, local.RECORD), "w") as f:
        json.dump(rec, f)
    return rec


def wav_of(path, samples):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(int(s).to_bytes(2, "little", signed=True) for s in samples))
    return path


class Pieces(unittest.TestCase):
    def test_close_spans_join_far_ones_stay_apart_with_edges(self):
        sr = 16000
        got = local.pieces([(sr, 2 * sr), (int(2.2 * sr), 3 * sr), (5 * sr, 6 * sr)], 7 * sr)
        self.assertEqual(got, [(int(0.8 * sr), int(3.2 * sr)), (int(4.8 * sr), int(6.2 * sr))])

    def test_a_long_stretch_is_cut_into_equal_parts_within_the_cap_without_overlap(self):
        sr = 16000
        got = local.pieces([(10 * sr, 70 * sr)], 80 * sr)
        self.assertEqual(len(got), 3)
        self.assertTrue(all(b - a <= 25 * sr for a, b in got), got)
        self.assertEqual((got[0][0], got[-1][1]), (int(9.8 * sr), int(70.2 * sr)))   # the edges only outside
        self.assertEqual([got[i][1] for i in range(2)], [got[i + 1][0] for i in range(2)])   # a word at a seam once

    def test_a_seam_moves_to_the_quietest_moment_near_it(self):
        sr = 16000
        x = [0.5] * (60 * sr)
        for i in range(int(20.3 * sr), int(20.34 * sr)):   # a breath 0.3 s after the equal seam at 20 s
            x[i] = 0.0
        got = local.pieces([(0, 60 * sr)], 60 * sr, x=x)
        self.assertTrue(abs(got[0][1] - int(20.3 * sr)) <= int(0.02 * sr), got)
        self.assertEqual(got[0][1], got[1][0])

    def test_no_speech_no_pieces(self):
        self.assertEqual(local.pieces([], 16000), [])
        self.assertEqual(local.pieces([(5, 5)], 16000), [])


class Installed(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="voice-state-")
        self.addCleanup(platforms.current.rmtree, self.state)

    def test_a_complete_record_is_installed(self):
        lay_out(self.state)
        rec = local.installed(self.state)
        self.assertEqual(rec["model"], "gigaam-v3-e2e-rnnt")
        self.assertTrue(os.path.isfile(rec["model_file"]))

    def test_nothing_a_gone_file_another_size_or_a_path_outside_is_not_installed(self):
        self.assertIsNone(local.installed(self.state))
        lay_out(self.state, model_size=999)
        self.assertIsNone(local.installed(self.state))   # the model's size is not the pinned one
        platforms.current.rmtree(local.runtime(self.state))
        rec = lay_out(self.state)
        os.remove(os.path.join(local.runtime(self.state), "voice-models", "v.gguf"))
        self.assertIsNone(local.installed(self.state))
        platforms.current.rmtree(local.runtime(self.state))
        rec = lay_out(self.state)
        rec["files"]["lib"]["path"] = "../../etc/x"
        with open(os.path.join(local.runtime(self.state), local.RECORD), "w") as f:
            json.dump(rec, f)
        self.assertIsNone(local.installed(self.state))


class Local(unittest.TestCase):
    """local.py as a process, the way transcribe.py runs it."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="voice-state-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.mark = os.path.join(self.state, "asr-loaded")
        self.env = dict(os.environ, FAKE_ASR_MARK=self.mark)

    def run_local(self, *args, env=None):
        r = subprocess.run([sys.executable, voice.LOCAL, *args, "--state", self.state], capture_output=True, text=True,
                           encoding="utf-8", env=env or self.env, stdin=subprocess.DEVNULL, timeout=60)
        return r.returncode, json.loads(r.stdout.strip().splitlines()[-1])

    def test_speech_is_recognized_piece_by_piece(self):
        lay_out(self.state)
        w = wav_of(os.path.join(self.state, "a.wav"), [0] * 16000 + [1000] * 16000 + [0] * 32000 + [1000] * 8000)
        code, out = self.run_local(w)
        self.assertEqual(code, 0, out)
        # two pieces (the pause is 2 s): their speech plus 0.2 s at both ends, within the file
        self.assertEqual(out, {"text": "w22400 w11200"})   # the last one ends at the end of the file

    def test_silence_gives_an_empty_text_and_the_model_is_not_loaded(self):
        lay_out(self.state)
        code, out = self.run_local(wav_of(os.path.join(self.state, "s.wav"), [0] * 32000))
        self.assertEqual((code, out), (0, {"text": ""}))
        self.assertFalse(os.path.exists(self.mark))

    def test_not_installed_is_model_missing(self):
        code, out = self.run_local(wav_of(os.path.join(self.state, "a.wav"), [1000] * 1600))
        self.assertEqual((code, out), (local.FAILED, {"error": "model_missing"}))

    def test_a_crash_of_the_model_is_engine_failed(self):
        lay_out(self.state)
        code, out = self.run_local(wav_of(os.path.join(self.state, "a.wav"), [1000] * 1600),
                                   env=dict(self.env, FAKE_ASR_FAIL="1"))
        self.assertEqual((code, out), (local.FAILED, {"error": "engine_failed"}))

    def test_check_loads_the_model(self):
        lay_out(self.state)
        self.assertEqual(self.run_local("--check"), (0, {"ok": True}))
        self.assertTrue(os.path.exists(self.mark))

    def test_a_model_that_does_not_tell_languages_gets_the_record_s_one(self):
        lay_out(self.state, language="ja")
        said = os.path.join(self.state, "lang")
        code, out = self.run_local(wav_of(os.path.join(self.state, "a.wav"), [1000] * 16000),
                                   env=dict(self.env, FAKE_ASR_LANG_MARK=said))
        self.assertEqual((code, out), (0, {"text": "w16000"}))
        self.assertEqual(open(said).read().split(), ["ja"])

    def test_check_recognizes_and_refuses_a_language_the_model_does_not_know(self):
        lay_out(self.state, language="xx")
        env = dict(self.env, FAKE_ASR_LANGS="ru,en")
        self.assertEqual(self.run_local("--check", env=env), (local.FAILED, {"error": "engine_failed"}))
        lay_out(self.state, language="ru")
        self.assertEqual(self.run_local("--check", env=env), (0, {"ok": True}))

    def test_the_record_is_read_under_the_lock(self):
        # a switch removes the earlier model's files under the recognition lock: a recording that waited for the lock
        # takes the model the record names then, not the one it named when the process started
        lay_out(self.state)
        w = wav_of(os.path.join(self.state, "a.wav"), [1000] * 1600)
        lock = os.path.join(local.runtime(self.state), local.LOCK)
        held, release = threading.Event(), threading.Event()

        def hold():
            with platforms.current.exclusive(lock):
                held.set()
                release.wait(30)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(10)
        try:
            p = subprocess.Popen([sys.executable, voice.LOCAL, w, "--state", self.state], stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True, env=dict(self.env, FAKE_ASR_TAG="1"),
                                 stdin=subprocess.DEVNULL)
            threading.Event().wait(2)   # the process is up and waits for the lock
            self.assertIsNone(p.poll())
            lay_out(self.state, model="parakeet-ultra", file="p.gguf")   # the switch: a new record, the old file gone
            os.remove(os.path.join(local.runtime(self.state), "voice-models", "m.gguf"))
        finally:
            release.set()
            t.join()
        out, err = p.communicate(timeout=60)
        self.assertEqual(json.loads(out.strip().splitlines()[-1]), {"text": "p.gguf:w1600"}, err)

    def test_a_held_lock_is_engine_busy(self):
        lay_out(self.state)
        w = wav_of(os.path.join(self.state, "a.wav"), [1000] * 1600)
        held, release = threading.Event(), threading.Event()

        def hold():
            with platforms.current.exclusive(os.path.join(local.runtime(self.state), local.LOCK)):
                held.set()
                release.wait(30)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(10)
        try:
            out = io.StringIO()
            with mock.patch.object(local, "LOCK_WAIT", 0.3), mock.patch("sys.stdout", out):
                code = local.main([w, "--state", self.state])
        finally:
            release.set()
            t.join()
        self.assertEqual((code, json.loads(out.getvalue())), (local.FAILED, {"error": "engine_busy"}))


class Transcribe(unittest.TestCase):
    """transcribe.py: the command first, the built-in model without one; its failures by their codes."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="voice-state-")
        self.addCleanup(platforms.current.rmtree, self.state)
        d = tempfile.mkdtemp(prefix="voice-")
        self.addCleanup(platforms.current.rmtree, d)
        # a fake ffmpeg: a WAV of 1 s of "speech" whatever comes in
        self.ffmpeg = script(os.path.join(d, "ffmpeg"),
                             "import sys, wave\nw = wave.open(sys.argv[-1], 'wb')\nw.setnchannels(1)\nw.setsampwidth(2)\n"
                             "w.setframerate(16000)\nw.writeframes(b'\\x10\\x00' * 16000)\nw.close()\n")
        self.cfg = {"voice": {"ffmpeg": self.ffmpeg}}

    def test_voice_is_enabled_by_the_built_in_model(self):
        self.assertFalse(voice.enabled(self.cfg, state=self.state))
        lay_out(self.state)
        self.assertTrue(voice.enabled(self.cfg, state=self.state))
        self.assertEqual(voice.transcribe(b"ogg", self.cfg, ".ogg", state=self.state), "w16000")

    def test_the_model_s_files_are_not_looked_at_before_the_lock(self):
        # the record is enough to run local.py: a file gone is its model_missing, under the lock (a switch may be on)
        lay_out(self.state)
        os.remove(os.path.join(local.runtime(self.state), "voice-models", "m.gguf"))
        self.assertTrue(voice.local_argv(self.state))
        with self.assertRaises(voice.VoiceError) as e:
            voice.transcribe(b"ogg", self.cfg, ".ogg", state=self.state)
        self.assertEqual(e.exception.kind, "model_missing")

    def test_the_command_comes_first(self):
        lay_out(self.state)
        asr = script(os.path.join(self.state, "asr"), "import json\nprint(json.dumps({'text': 'by command'}))\n")
        cfg = {"voice": {**self.cfg["voice"], "cmd": f"{asr} {{wav}}"}}
        self.assertEqual(voice.transcribe(b"ogg", cfg, ".ogg", state=self.state), "by command")

    def test_a_failure_of_the_model_has_its_code(self):
        lay_out(self.state, model_size=5)   # the record no longer matches the file: as if it were removed by hand
        os.remove(os.path.join(local.runtime(self.state), local.RECORD))
        with self.assertRaises(voice.VoiceError):
            voice.transcribe(b"ogg", self.cfg, ".ogg", state=self.state)
        lay_out_again = lay_out
        platforms.current.rmtree(local.runtime(self.state))
        lay_out_again(self.state)
        with mock.patch.dict(os.environ, FAKE_ASR_FAIL="1"):
            with self.assertRaises(voice.VoiceError) as e:
                voice.transcribe(b"ogg", self.cfg, ".ogg", state=self.state)
        self.assertEqual(e.exception.kind, "engine_failed")

    def test_only_busy_is_retried(self):
        lay_out(self.state)
        calls, sleeps = [], []

        def fake(audio, cfg, suffix=".ogg", cancel=None, deadline=None, state=None):
            calls.append(1)
            err = voice.VoiceError("x")
            err.kind = "engine_busy" if len(calls) < 3 else "model_missing"
            raise err
        with mock.patch.object(voice, "transcribe", fake):
            with self.assertRaises(voice.VoiceError) as e:
                voice.transcribe_retry(b"ogg", self.cfg, ".ogg", sleep=sleeps.append, state=self.state)
        self.assertEqual((len(calls), sleeps, e.exception.kind), (3, [10, 30], "model_missing"))


class CloudCut(unittest.TestCase):
    """The open flavor has no cloud row (core/appcode.OPEN_CUT: modules/voice/chatgpt.py): no row, a choice left on disk
    routes nothing, the page signs a Claude hub in to Codex for nobody. Runs on both trees: HUB_TEST_CUT makes this one
    the open one; on the cut tree the file is not there at all."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="voice-cut-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.enterContext(mock.patch.dict(os.environ, {"HUB_TEST_CUT": "voice_cloud"}))
        os.makedirs(local.runtime(self.state))
        with open(os.path.join(local.runtime(self.state), "voice-cloud.json"), "w") as f:
            json.dump({"on": True}, f)   # as if chosen before the hub moved to the open flavor

    def test_no_cloud_row_and_a_left_choice_routes_nothing(self):
        self.assertNotIn("chatgpt", [r["id"] for r in install.rows("ru", self.state)])
        self.assertIsNone(voice.cloud_argv(self.state))
        self.assertFalse(voice.enabled({}, state=self.state))
        self.assertFalse(install.status(self.state, "ru").get("on"))
        lay_out(self.state)   # a local model: it alone recognizes
        self.assertEqual((voice.cloud_argv(self.state), voice.local_argv(self.state)[1]), (None, voice.LOCAL))

    def test_the_step_refuses_the_cloud_row(self):
        from modules.setup import job as setup_job
        made = []

        class Job:
            state, arg = self.state, "chatgpt"

            def set(self, **kw):
                pass

            def finish(self, state, **kw):
                made.append((state, kw.get("why")))
        setup_job.step_voice(Job())
        self.assertEqual(made, [("failed", "model")])

    def test_the_page_does_not_sign_a_claude_hub_in(self):
        from tests import test_web
        w = test_web.WebTest("run")
        w.setUp()
        self.addCleanup(w.tearDown)
        with mock.patch("core.codexcli.start_login", side_effect=AssertionError("no sign-in")):
            code, _ = w.req("POST", "/api/codex/login", {"for": "voice"})
        self.assertEqual(code, 400)
        code, _ = w.req("POST", "/api/voice/local", {"on": True, "model": "chatgpt"})
        self.assertEqual(code, 400)


def _tgz(files, top="transcribe-native-linux-x86_64-cpu-vulkan", link=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
        if link:
            info = tarfile.TarInfo(f"{top}/{link[0]}")
            info.type, info.linkname = tarfile.SYMTYPE, link[1]
            t.addfile(info)
    return buf.getvalue()


def _wheel():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("transcribe_cpp/__init__.py", FAKE)
        z.writestr("transcribe_cpp-0.4.1.dist-info/METADATA", "x")
    return buf.getvalue()


class Install(unittest.TestCase):
    """The setup step's download: every file by its pin; checked by loading before voice.json is written."""

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="voice-state-")
        self.addCleanup(platforms.current.rmtree, self.state)
        self.blobs = {"https://x.test/native.tgz": _tgz({"libtranscribe.so": b"lib", "licenses/LICENSE": b"MIT"}),
                      "https://x.test/w.whl": _wheel(), "https://x.test/m.gguf": b"model!", "https://x.test/v.gguf": b"vad",
                      "https://x.test/p.gguf": b"other model"}
        pin = lambda url: {"url": url, "sha256": hashlib.sha256(self.blobs[url]).hexdigest(), "size": len(self.blobs[url])}
        self.engine = {"version": "0.4.1", "lib": pin("https://x.test/native.tgz"), "wrapper": pin("https://x.test/w.whl"),
                       "platform": "linux-x64"}
        self.rows = {"gigaam-v3-e2e-rnnt": {**pin("https://x.test/m.gguf"), "id": "gigaam-v3-e2e-rnnt", "name": "GigaAM v3",
                                            "file": "m.gguf", "lang": "ru"},
                     "parakeet-ultra": {**pin("https://x.test/p.gguf"), "id": "parakeet-ultra", "name": "Parakeet Ultra",
                                        "file": "p.gguf", "lang": "ru", "language": "ru"}}
        self.vad = {**pin("https://x.test/v.gguf"), "id": "silero", "file": "v.gguf"}

        def model_pin(lang, model=None):
            row = self.rows.get(model or "gigaam-v3-e2e-rnnt") if lang == "ru" else None
            return {"model": row, "vad": self.vad} if row else None
        self.fetched = []

        def download(url, dest, sha, size, *a, **kw):
            self.fetched.append(url)
            data = self.blobs[url]
            assert hashlib.sha256(data).hexdigest() == sha and len(data) == size
            open(dest, "wb").write(data)
            return dest
        for p in (mock.patch.object(install, "engine_pin", lambda plat=None: self.engine),
                  mock.patch.object(install, "model_pin", model_pin),
                  mock.patch("core.fetch.download", download)):
            p.start()
            self.addCleanup(p.stop)

    def test_downloads_checks_and_names_it(self):
        rec = install.install(self.state, "ru")
        self.assertEqual(rec["model"], "gigaam-v3-e2e-rnnt")
        self.assertEqual(local.installed(self.state)["lang"], "ru")
        self.assertEqual(len(self.fetched), 4)
        self.assertFalse(os.listdir(os.path.join(self.state, "downloads")))   # the archives are gone after unpacking
        install.install(self.state, "ru")   # again: the engine is unpacked, the model and the detector are there
        self.assertEqual(len(self.fetched), 4)

    def test_a_check_that_does_not_load_leaves_voice_off(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 3, '{"error": "engine_failed"}', "GGML_ASSERT")
        with self.assertRaises(install.Refused) as e:
            install.install(self.state, "ru", run=run)
        self.assertEqual(e.exception.why, "check")
        self.assertIsNone(local.installed(self.state))
        self.assertFalse(os.path.exists(os.path.join(local.runtime(self.state), local.RECORD + ".check")))

    def test_a_cancel_after_the_last_block_leaves_voice_off(self):
        with self.assertRaises(install.Refused) as e:
            install.install(self.state, "ru", cancelled=lambda: True)
        self.assertEqual(e.exception.why, "cancelled")
        self.assertIsNone(local.installed(self.state))

    def test_without_ffmpeg_nothing_is_downloaded(self):
        with mock.patch.object(install, "has_ffmpeg", lambda cfg: False):
            with self.assertRaises(install.Refused) as e:
                install.install(self.state, "ru", cfg={})
        self.assertEqual((e.exception.why, self.fetched), ("ffmpeg", []))

    def test_off_pressed_after_the_last_check_removes_what_came(self):
        from modules.setup import job
        j = job.Job(self.state, "voice")
        with mock.patch.object(install, "has_ffmpeg", lambda cfg: True), mock.patch("core.i18n.current", lambda: "ru"), \
                mock.patch.object(job.Job, "cancelled", lambda self: os.path.exists(os.path.join(self.dir, "voice.cancel"))):
            real = install.install

            def late(*a, **kw):   # the install finishes, then «Turn off» lands
                rec = real(*a, **kw)
                open(os.path.join(j.dir, "voice.cancel"), "w").close()
                return rec
            with mock.patch.object(install, "install", late):
                job.step_voice(j)
        self.assertEqual(j.st["state"], "cancelled")
        self.assertIsNone(local.installed(self.state))
        self.assertFalse(os.path.exists(os.path.join(local.runtime(self.state), "voice-models")))

    def test_off_pressed_before_the_step_started_is_not_lost(self):
        # «Turn on», then «Turn off» before the step's process wrote its file: its own start drops a .cancel, the mark
        # stays — the step ends cancelled and nothing is installed; the next «Turn on» clears the mark
        from modules.setup import job
        install.mark_off(self.state)
        j = job.Job(self.state, "voice")
        with mock.patch.object(install, "has_ffmpeg", lambda cfg: True), mock.patch("core.i18n.current", lambda: "ru"):
            job.step_voice(j)
            self.assertEqual((j.st["state"], local.installed(self.state)), ("cancelled", None))
            install.clear_off(self.state)
            job.step_voice(job.Job(self.state, "voice"))
        self.assertIsNotNone(local.installed(self.state))

    def test_a_language_without_a_model_is_refused_before_any_download(self):
        with self.assertRaises(install.Refused) as e:
            install.install(self.state, "ja")
        self.assertEqual((e.exception.why, self.fetched), ("lang", []))

    def test_an_archive_with_a_link_or_a_path_outside_is_refused(self):
        for bad in (_tgz({"libtranscribe.so": b"lib"}, link=("libx.so", "/etc/passwd")),
                    _tgz({"../evil.so": b"x", "libtranscribe.so": b"lib"})):
            with self.subTest():
                self.blobs["https://x.test/native.tgz"] = bad
                self.engine["lib"] = {"url": "https://x.test/native.tgz", "sha256": hashlib.sha256(bad).hexdigest(),
                                      "size": len(bad)}
                with self.assertRaises(install.Refused):
                    install.install(self.state, "ru")
                self.assertIsNone(local.installed(self.state))

    def models_dir(self):
        return os.path.join(local.runtime(self.state), "voice-models")

    def test_a_switch_keeps_the_engine_and_removes_the_earlier_model_after_the_check(self):
        install.install(self.state, "ru")
        lib = os.path.join(local.runtime(self.state), "voice", "0.4.1", "native", "libtranscribe.so")
        os.utime(lib, ns=(1_000_000_000_000_000_000, 1_000_000_000_000_000_000))   # a time no unpack would give
        before = os.stat(lib).st_mtime_ns   # portable: the time is set above, not left to the disk's tick
        named = []

        def run(argv, **kw):   # the new model's check: the record still names the model in use
            named.append(local.read_record(self.state)["model"])
            return subprocess.CompletedProcess(argv, 0, '{"ok": true}', "")
        rec = install.install(self.state, "ru", model="parakeet-ultra", run=run)
        self.assertEqual(named, ["gigaam-v3-e2e-rnnt"])
        self.assertEqual((rec["model"], rec["language"]), ("parakeet-ultra", "ru"))
        self.assertEqual(sorted(os.listdir(self.models_dir())), ["p.gguf", "v.gguf"])
        self.assertEqual(self.fetched[4:], ["https://x.test/p.gguf"])   # only the model: the engine is in place
        self.assertEqual(os.stat(lib).st_mtime_ns, before)   # portable: set by os.utime, an unpack would replace the file

    def step(self, model, run=subprocess.run, cancel=False):
        from modules.setup import job
        j = job.Job(self.state, "voice")
        j.arg = model
        if cancel:
            open(os.path.join(j.dir, "voice.cancel"), "w").close()
        with mock.patch.object(install, "has_ffmpeg", lambda cfg: True), mock.patch("core.i18n.current", lambda: "ru"):
            job.step_voice(j, run=run)
        return j.st

    def test_a_switch_whose_model_does_not_load_keeps_the_one_in_use(self):
        install.install(self.state, "ru")

        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 3, '{"error": "engine_failed"}', "unsupported language")
        st = self.step("parakeet-ultra", run=run)
        self.assertEqual((st["state"], st["why"], st["model"]), ("failed", "check", "parakeet-ultra"))
        self.assertEqual(local.installed(self.state)["model"], "gigaam-v3-e2e-rnnt")
        self.assertEqual(sorted(os.listdir(self.models_dir())), ["m.gguf", "v.gguf"])

    def test_a_check_stopped_by_a_busy_lock_keeps_the_downloaded_files(self):
        # review: engine_busy (a long recording held the lock), a time-out or a kill is not "does not load": the file
        # stays for «Again», on a switch and on a first turn-on alike
        def busy(argv, **kw):
            return subprocess.CompletedProcess(argv, 3, '{"error": "engine_busy"}', "")
        st = self.step("gigaam-v3-e2e-rnnt", run=busy)
        self.assertEqual((st["state"], st["why"]), ("failed", "check_later"))
        self.assertEqual(sorted(os.listdir(self.models_dir())), ["m.gguf", "v.gguf"])
        install.install(self.state, "ru")
        st = self.step("parakeet-ultra", run=busy)
        self.assertEqual(st["why"], "check_later")
        self.assertEqual(sorted(os.listdir(self.models_dir())), ["m.gguf", "p.gguf", "v.gguf"])
        self.assertEqual(local.installed(self.state)["model"], "gigaam-v3-e2e-rnnt")

    def test_cancel_pressed_before_the_step_started_keeps_the_one_in_use(self):
        # review: «Cancel» lands before the step's process wrote its file (its start drops a .cancel): the mark holds it
        install.install(self.state, "ru")
        install.mark_stop(self.state)
        st = self.step("parakeet-ultra")
        self.assertEqual(st["state"], "cancelled")
        self.assertEqual(local.installed(self.state)["model"], "gigaam-v3-e2e-rnnt")
        install.clear_off(self.state)
        self.assertFalse(install.stop_marked(self.state))

    def test_prune_leaves_what_will_not_go(self):
        install.install(self.state, "ru")
        os.makedirs(os.path.join(self.models_dir(), "odd"))   # not a file: os.remove refuses it
        self.assertEqual(install.prune(self.state), [])
        self.assertEqual(local.installed(self.state)["model"], "gigaam-v3-e2e-rnnt")

    def test_a_record_whose_files_are_gone_is_on_and_says_so(self):
        # review: recordings are routed to it (transcribe.enabled), so the page must not show voice off
        install.install(self.state, "ru")
        os.remove(os.path.join(self.models_dir(), "m.gguf"))
        st = install.status(self.state, "ru", plat="linux-x64")
        self.assertEqual((st["on"], st.get("missing"), st["model"]), (True, True, "gigaam-v3-e2e-rnnt"))

    def test_a_cancelled_switch_keeps_the_one_in_use(self):
        install.install(self.state, "ru")
        st = self.step("parakeet-ultra", cancel=True)
        self.assertEqual(st["state"], "cancelled")
        self.assertEqual(local.installed(self.state)["model"], "gigaam-v3-e2e-rnnt")
        self.assertEqual(sorted(os.listdir(self.models_dir())), ["m.gguf", "v.gguf"])

    def test_off_during_a_switch_removes_everything(self):
        install.install(self.state, "ru")
        install.mark_off(self.state)
        st = self.step("parakeet-ultra")
        self.assertEqual(st["state"], "cancelled")
        self.assertIsNone(local.installed(self.state))
        self.assertFalse(os.path.exists(self.models_dir()))

    def test_a_model_not_among_the_language_s_rows_is_refused(self):
        with self.assertRaises(install.Refused) as e:
            install.install(self.state, "ru", model="whisper-small")
        self.assertEqual((e.exception.why, self.fetched), ("model", []))

    def test_not_enough_disk_space_downloads_nothing(self):
        usage = collections.namedtuple("usage", "total used free")
        with mock.patch("shutil.disk_usage", lambda path: usage(100, 90, 10)):
            with self.assertRaises(install.Refused) as e:
                install.install(self.state, "ru")
        self.assertEqual((e.exception.why, self.fetched), ("space", []))

    def test_the_earlier_model_waits_for_a_recording_on_it(self):
        install.install(self.state, "ru")
        open(os.path.join(self.models_dir(), "old.gguf"), "wb").write(b"x")
        held, release = threading.Event(), threading.Event()

        def hold():
            with platforms.current.exclusive(os.path.join(local.runtime(self.state), local.LOCK)):
                held.set()
                release.wait(30)
        t = threading.Thread(target=hold)
        t.start()
        held.wait(10)
        try:
            self.assertEqual(install.prune(self.state, wait=0.3), [])
            self.assertIn("old.gguf", os.listdir(self.models_dir()))
        finally:
            release.set()
            t.join()
        self.assertEqual(install.prune(self.state), ["old.gguf"])

    def test_remove_turns_it_off(self):
        install.install(self.state, "ru")
        self.assertTrue(install.remove(self.state))
        self.assertIsNone(local.installed(self.state))
        self.assertFalse(os.path.exists(os.path.join(local.runtime(self.state), "voice-models")))
        self.assertFalse(install.remove(self.state))


class StepArg(unittest.TestCase):
    """The model a person chose goes to the setup step as its process's argument: the step's lock makes the one that runs
    the one whose model the page shows (two tabs cannot swap a choice under it)."""

    def test_the_model_goes_to_the_step_s_process(self):
        from modules.setup import job
        from modules.setup.setup import Setup
        state = tempfile.mkdtemp(prefix="voice-state-")
        self.addCleanup(platforms.current.rmtree, state)
        spawned = []
        s = Setup(state, lambda: {}, spawn=lambda argv, env: spawned.append(argv))
        self.assertEqual(s.run("voice", "ru", arg="parakeet-ultra")[0], 202)
        self.assertEqual(spawned[-1][-2:], ["voice", "parakeet-ultra"])
        self.assertEqual(s.run("voice", "ru", arg="../etc")[0], 400)
        self.assertEqual(s.run("doctor", "ru", arg="x")[0], 400)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(job.main([state, "voice", "../etc"]), 2)

    def test_a_start_clears_an_earlier_cancel_or_off(self):
        # review: «Cancel» with no step running left its mark; any later start of the step (the setup list included)
        # must not be stopped by it
        from modules.setup.setup import Setup
        state = tempfile.mkdtemp(prefix="voice-state-")
        self.addCleanup(platforms.current.rmtree, state)
        install.mark_stop(state)
        install.mark_off(state)
        Setup(state, lambda: {}, spawn=lambda argv, env: None).run("voice", "ru")
        self.assertFalse(install.stop_marked(state) or install.off_marked(state))


class Choices(unittest.TestCase):
    def test_the_engine_follows_the_interpreter_s_architecture(self):
        # an x86_64 Python under Rosetta on an arm Mac loads x64 libraries only (chrome.key would say mac-arm64)
        with mock.patch("sys.platform", "darwin"), mock.patch("platform.machine", lambda: "x86_64"), \
                mock.patch("modules.setup.chrome._translated", lambda: True):
            self.assertEqual(install.engine_pin()["platform"], "mac-x64")

    def test_no_ffmpeg_no_offer(self):
        with mock.patch.object(install, "has_ffmpeg", lambda cfg: False):
            self.assertEqual(install.offer("ru", "linux-x64", cfg={}), {"why": "ffmpeg"})
        self.assertEqual(install.offer("ru", "linux-x64")["name"], "GigaAM v3")   # without cfg: not asked


class Pins(unittest.TestCase):
    """The real pins: every engine build and the model by https, sha256 and size; Russian has a model."""

    def test_the_engine_is_pinned_for_five_platforms_and_not_for_windows_arm(self):
        for plat in ("linux-x64", "linux-arm64", "mac-arm64", "mac-x64", "windows-x64"):
            pin = install.engine_pin(plat)
            self.assertTrue(pin and pin["lib"]["url"].startswith("https://github.com/handy-computer/"), plat)
        self.assertIsNone(install.engine_pin("windows-arm64"))

    def test_every_hub_language_has_models_the_recommended_first(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "i18n", "stage.json")) as f:
            ready = json.load(f)["ready"]
        self.assertEqual(len(ready), 13)
        for lang in ready:
            rows = install.choices(lang)
            with self.subTest(lang=lang):
                self.assertGreaterEqual(len(rows), 2)
                self.assertEqual(rows[0]["role"], "recommended")
                for r in rows:
                    self.assertTrue(r["url"].startswith("https://huggingface.co/handy-computer/"), r["id"])
                    self.assertRegex(r["sha256"], r"^[0-9a-f]{64}$")
                    self.assertEqual(r["lang"], install.base(lang))
                self.assertNotIn("why", install.offer(lang, "linux-x64"))
        self.assertEqual(install.model_pin("ru")["model"]["id"], "gigaam-v3-e2e-rnnt")
        self.assertEqual(install.offer("ru", "windows-arm64"), {"why": "platform"})

    def test_a_model_that_does_not_tell_languages_is_given_the_hub_s(self):
        lang = {lang: {r["id"]: r["language"] for r in install.choices(lang)} for lang in ("ar", "ja", "ru", "en")}
        self.assertEqual(lang["ar"]["cohere-transcribe-arabic-07-2026"], "ar")   # 40 % errors without it (§5.1)
        self.assertEqual(lang["ja"]["Fun-ASR-MLT-Nano-2512"], "ja")
        self.assertIsNone(lang["ru"]["gigaam-v3-e2e-rnnt"])   # one language: nothing to tell it
        self.assertIsNone(lang["en"]["parakeet-ultra"])   # it tells languages itself: mixed speech stays its own
        self.assertEqual(lang["en"]["whisper-small"], "en")   # left to itself it took accented English for Welsh

    def test_regional_variants_are_one_language(self):
        # Nemotron lists en-US and en-GB, es-US and es-ES…: 32 codes, 28 languages (review)
        row = next(r for r in install.choices("tr") if r["id"] == "nemotron-3.5-asr-streaming-0.6b")
        self.assertEqual((row["langs"], row["english"]), (28, True))

    def test_the_first_release_form_stays_for_an_older_page_server(self):
        with open(install.MODELS, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["models"]["ru"]["sha256"], data["list"]["gigaam-v3-e2e-rnnt"]["sha256"])


if __name__ == "__main__":
    unittest.main()
