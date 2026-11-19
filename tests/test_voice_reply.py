"""The "Voice reply" module (optional, off by default): a reply to the owner's Telegram message goes as text, followed by
a voice reply to his message; if the voice fails, a quiet line, the text is already with him. The switch is in
"Settings -> Modules". The live voice (Codex realtime) is replaced here."""
import tests  # noqa: F401 — hide the machine's settings
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from core import journal, spool
from modules.telegram import bot as tg
from modules.voice import speech

speak = speech.speak   # None: the open flavor's run has no built-in voice (core/cutout.py)
from tests import test_web, winsafe
from tests.test_telegram import OWNER, FakeBotApi
from tests.fakes import exe
from tests.fakes import cut

ON = {"voice_reply": {"enabled": True}}


@unittest.skipUnless(speak, "the built-in voice is cut out of this tree")
class Speak(unittest.TestCase):
    def test_off_by_default_with_cove_v3(self):
        self.assertFalse(speak.enabled({}))
        self.assertFalse(speak.enabled({"voice_reply": {}}))
        self.assertTrue(speak.enabled(ON))
        self.assertEqual({k: speak.settings({})[k] for k in ("voice", "version")}, {"voice": "cove", "version": "v3"})

    def test_without_a_model_speech_is_the_answer_without_markup(self):
        # HUB_NO_MODEL (tests/__init__): Haiku is not called, so the start of the reply is spoken without markup, paths,
        # code and links
        text = ("**Done**: fixed `modules/x.py`, the test in /home/x/tests/t.py is green. Details — "
                "[in the PR](https://example.com/pr/1).\n```\ncode\n```\n- Enable it — in the settings.")
        said = speak.brief(text)
        self.assertEqual(said, "Done: fixed, the test in is green. Details — in the PR. Enable it — in the settings.")
        long = " ".join(f"Phrase number {i} on it." for i in range(60))
        self.assertLess(len(speak.brief(long).split()), 70)   # 10-40 s of speech, not the whole reply

    def test_retries_when_the_voice_strays_and_keeps_the_best_take(self):
        tries = []

        def voice(text, out, s):
            tries.append(s["voice"])
            with open(out, "wb") as f:
                f.write(b"webm")
            return "something else entirely and many more words on top" if len(tries) == 1 else text

        ff = exe.script(os.path.join(tempfile.mkdtemp(), "ffmpeg"),
                        "import sys, shutil\nshutil.copy(sys.argv[sys.argv.index('-i') + 1], sys.argv[-1])\n")
        ogg, secs, said = speak.reply("Done. Please check.", {"voice": {"ffmpeg": ff}}, voice=voice)
        self.assertEqual((len(tries), said, os.path.basename(ogg)), (2, "Done. Please check.", "reply.ogg"))
        self.assertTrue(os.path.isfile(ogg))
        shutil.rmtree(os.path.dirname(ogg))

    def test_no_voice_at_all_is_an_error_and_leaves_nothing(self):
        def voice(text, out, s):
            raise speech.SpeakError("Codex realtime: not logged in")

        before = set(os.listdir(tempfile.gettempdir()))
        with self.assertRaisesRegex(speech.SpeakError, "not logged in"):
            speak.reply("Done.", {}, voice=voice)
        self.assertEqual({n for n in os.listdir(tempfile.gettempdir()) if n.startswith("hub-speak-")} - before, set())

    def test_live_voice_is_not_called_in_tests(self):
        with self.assertRaisesRegex(speech.SpeakError, "HUB_NO_MODEL"):
            speak.realtime("Done.", os.path.join(tempfile.mkdtemp(), "x.webm"), speak.settings({}))


@cut.needs("speech")
class CodexPipe(unittest.TestCase):
    """`codex app-server` as a real child process (a fake program): what the voice reads from it and how it stops it."""

    def rpc(self, body):
        r = speak.Rpc(exe.script(os.path.join(tempfile.mkdtemp(), "codex"), "import sys\n" + body))
        self.addCleanup(r.close)
        return r

    def test_a_closed_conversation_or_a_final_error_fails_at_once(self):
        """Behind a proxy the server closed the conversation in about 3 s; the voice waited for its 20-180 s instead."""
        lines = [{"method": "error", "params": {"error": {"message": "busy"}, "willRetry": True}},
                 {"method": "error", "params": {"error": {"message": "conversation is not running"},
                                                "willRetry": False}}]
        r = self.rpc("".join(f"print({json.dumps(json.dumps(m))}, flush=True)\n" for m in lines) + "sys.stdin.read()\n")
        # its own reason, not "did not answer" at the end of the wait; the retried error is passed over
        with self.assertRaisesRegex(speak.SpeakError, "conversation is not running"):
            r.wait(method="thread/realtime/started", timeout=10)
        closed = {"method": "thread/realtime/closed", "params": {"threadId": "t", "reason": "error"}}
        r = self.rpc(f"print({json.dumps(json.dumps(closed))}, flush=True)\nsys.stdin.read()\n")
        with self.assertRaisesRegex(speak.SpeakError, "closed.*error"):
            r.wait(method="thread/realtime/started", timeout=10)

    def test_a_byte_that_is_not_utf8_does_not_stop_the_reader(self):
        r = self.rpc("sys.stdout.buffer.write(b'{\"method\": \"x\", \"params\": {\"t\": \"\\xff\"}}\\n')\n"
                     "sys.stdout.buffer.write(b'{\"method\": \"y\", \"params\": {\"t\": \"\\xd0\\x93\"}}\\n')\n"
                     "sys.stdout.flush()\nsys.stdin.read()\n")
        self.assertIn("\ufffd", r.wait(method="x", timeout=10)["params"]["t"])
        self.assertEqual(r.wait(method="y", timeout=10)["params"]["t"], "\u0413")

    def test_the_pipe_is_utf8_in_a_hub_whose_locale_is_not(self):
        """Windows: the pipe in the locale's code page turned a Cyrillic transcript into garbage or killed the reader.
        Here a hub process in the C locale (not UTF-8, as a code page) reads the same Cyrillic line."""
        env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0",
               "PYTHONIOENCODING": "", "PYTHONPATH": os.path.dirname(os.path.dirname(os.path.abspath(__file__)))}
        run = [sys.executable, "-X", "utf8=0", "-c"]
        enc = subprocess.run(run + ["import locale; print(locale.getencoding())"], env=env, capture_output=True,
                             text=True, timeout=30).stdout.strip()
        if enc.lower().replace("-", "") == "utf8":
            self.skipTest("the C locale of this machine is UTF-8")
        fake = exe.script(os.path.join(tempfile.mkdtemp(), "codex"),
                          "import sys\nsys.stdout.buffer.write(b'{\"method\": \"y\", \"params\": {\"t\": \"\\xd0\\x93\"}}"
                          "\\n')\nsys.stdout.flush()\nsys.stdin.read()\n")
        probe = ("import sys\nfrom modules.voice import speak\nr = speak.Rpc(sys.argv[1])\n"
                 "try:\n    print(ascii(r.wait(method='y', timeout=10)['params']['t']))\n"
                 "except speak.SpeakError as e:\n    print('error', ascii(str(e)))\nfinally:\n    r.close()\n")
        got = subprocess.run(run + [probe, fake], env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(got.stdout.strip(), ascii("\u0413"), got.stderr[-500:])

    def test_close_stops_the_whole_tree(self):
        """codex from npm is a wrapper -> node -> the codex binary; terminate() stopped only the wrapper."""
        from platforms import current
        r = self.rpc("import json, subprocess\n"
                     "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                     "print(json.dumps({'method': 'pid', 'params': {'pid': c.pid}}), flush=True)\nsys.stdin.read()\n")
        pid = r.wait(method="pid", timeout=20)["params"]["pid"]
        self.addCleanup(lambda: current.alive(pid) and os.kill(pid, 9))   # the child must not outlive a failed test
        self.assertTrue(current.alive(pid))
        r.close()
        for _ in range(50):
            if not current.alive(pid):
                break
            time.sleep(0.1)
        self.assertFalse(current.alive(pid))

    def test_close_kills_the_child_when_the_platform_layer_fails(self):
        r = self.rpc("sys.stdin.read()\n")
        with mock.patch("platforms.current.kill_detached", side_effect=ImportError("gone")):
            r.close()
        self.assertIsNotNone(r.proc.wait(5))


class FakeClock:
    """time.time / time.sleep of speak without waiting: every wait moves the clock."""

    def __init__(self):
        self.now = 1000.0

    def time(self):
        return self.now

    def sleep(self, n):
        self.now += n


class FakePage:
    def __init__(self, closed):
        self.closed = closed

    def js(self, code):
        if code is speak.PAGE_JS:
            return "v=0 sdp"
        if code is speak.STOP_JS:
            return "d2Vibg=="   # base64 of "webn"
        return {"window.__pc.connectionState": "connected", "window.__lastLoud": 0}.get(code, 1)

    def goto(self, *a, **kw):
        pass

    def close(self):
        self.closed.append("chrome")


class FakeRpc(speak.Rpc if speak else object):   # no built-in voice: never used (the tests skip)
    """`codex app-server` at the JSON-RPC lines: replies and notifications go into Rpc's own queue, so reading, the
    errors and the waiting are the real code. `start` — what comes after thread/realtime/start (by default the answer
    and started), `events` — after the text is given (appendText, or appendSpeech when appendText is unknown); None is
    the server's exit; `stop` — what writing thread/realtime/stop raises; `refuse` — {method: the error it answers}."""

    def __init__(self, clock, events, start=None, stop=None, refuse=None):
        self.clock, self.events, self.start, self.stop, self.refuse = clock, list(events), start, stop, refuse or {}
        self.q, self.n, self.sent, self.closed, self.given = __import__("queue").Queue(), 0, [], False, []

    def send(self, method, params=None, notify=False):
        if notify:
            return self.n
        if method == "thread/realtime/stop" and self.stop:
            raise self.stop
        self.n += 1
        self.sent.append(method)
        if method.startswith("thread/realtime/append"):
            self.given.append((method, (params or {}).get("text")))
        if method in self.refuse:
            self.q.put({"id": self.n, "error": self.refuse[method]})
            return self.n
        self.q.put({"id": self.n, "result": {"thread": {"id": "t1"}} if method == "thread/start" else {}})
        if method == "thread/realtime/start":
            for ev in self.start or [{"method": "thread/realtime/sdp", "params": {"sdp": "answer"}},
                                     {"method": "thread/realtime/started", "params": {}}]:
                self.q.put(ev)
        if method.startswith("thread/realtime/append"):
            for ev in self.events:
                self.q.put(ev)
        return self.n

    def next(self, timeout):
        if self.q.empty():
            self.clock.now += timeout   # nothing to read: the time passes without waiting
            return {}
        return super().next(timeout)

    def close(self):
        self.closed = True


def delta(text):
    return {"method": "thread/realtime/transcript/delta", "params": {"delta": text}}


DONE = {"method": "thread/realtime/transcript/done", "params": {}}
CLOSED = {"method": "thread/realtime/closed", "params": {"threadId": "t1", "reason": "ended"}}


@cut.needs("speech")
class RealtimeTake(unittest.TestCase):
    """realtime() with a fake Codex app server, page and clock: what a closing or a broken pipe does to the take."""

    def take(self, events=(), rpc=None, **kw):
        self.chrome = []
        clock = rpc.clock if rpc else FakeClock()
        self.fake = rpc or FakeRpc(clock, events, **kw)
        out = os.path.join(tempfile.mkdtemp(), "x.webm")
        from modules.proof import cdp
        with mock.patch.dict(os.environ, {"HUB_NO_MODEL": ""}), mock.patch.object(speak, "time", clock), \
                mock.patch.object(speak, "Rpc", lambda exe, env=None: self.fake), \
                mock.patch.object(speak, "codex", return_value="codex"), \
                mock.patch.object(cdp, "Page", lambda **kw: FakePage(self.chrome)):
            try:
                return (speak.realtime("Done.", out, speak.settings({})), out)
            finally:
                self.assertEqual(self.chrome, ["chrome"])

    def test_a_server_gone_after_the_last_word_keeps_the_take(self):
        said, out = self.take([delta("Done."), DONE, None])
        self.assertEqual((said, os.path.getsize(out)), ("Done.", 4))
        self.assertTrue(self.fake.closed)

    def test_a_broken_pipe_on_stop_keeps_the_take(self):
        """codex already exited after the take: writing stop raised BrokenPipeError, and the recorded voice was lost."""
        said, out = self.take([delta("Done."), DONE], stop=BrokenPipeError(32, "Broken pipe"))
        self.assertEqual((said, os.path.getsize(out)), ("Done.", 4))

    def test_a_conversation_closed_in_the_middle_of_the_speech_fails(self):
        with self.assertRaisesRegex(speak.SpeakError, "closed.*ended"):
            self.take([delta("Do"), CLOSED])

    def test_a_conversation_closed_before_it_started_fails_before_the_text_is_given(self):
        with self.assertRaisesRegex(speak.SpeakError, "closed.*ended"):
            self.take(start=[{"method": "thread/realtime/sdp", "params": {"sdp": "answer"}}, CLOSED])
        self.assertEqual(self.fake.given, [])

    def test_a_final_error_before_it_started_keeps_its_reason(self):
        final = {"method": "error", "params": {"error": {"message": "quota is over"}, "willRetry": False}}
        with self.assertRaisesRegex(speak.SpeakError, "quota is over"):
            self.take(start=[{"method": "thread/realtime/sdp", "params": {"sdp": "answer"}}, final])
        self.assertEqual(self.fake.given, [])

    def test_the_text_is_given_to_read_aloud_not_as_a_turn_to_answer(self):
        # appendSpeech (and bare appendText) made the voice answer the text ("Yes, I hear you") instead of reading it;
        # appendText inside a read-aloud frame read it word for word (modules PR #52)
        said, _ = self.take([delta("Done."), DONE])
        self.assertEqual(said, "Done.")
        self.assertEqual(self.fake.given, [("thread/realtime/appendText",
                                            "Read this text aloud word for word, nothing else:\n\nDone.")])

    def test_a_codex_without_append_text_gets_the_bare_text_as_speech(self):
        unknown = {"code": -32601, "message": "Method not found: thread/realtime/appendText"}
        said, _ = self.take([delta("Done."), DONE], refuse={"thread/realtime/appendText": unknown})
        self.assertEqual(said, "Done.")
        self.assertEqual(self.fake.given[-1], ("thread/realtime/appendSpeech", "Done."))

    def test_another_refusal_of_the_text_is_not_retried_as_speech(self):
        busy = {"code": -32000, "message": "thread is busy"}
        with self.assertRaisesRegex(speak.SpeakError, "thread is busy"):
            self.take([delta("Done."), DONE], refuse={"thread/realtime/appendText": busy})
        self.assertEqual([m for m, _ in self.fake.given], ["thread/realtime/appendText"])

    def test_chrome_is_closed_even_if_stopping_codex_fails(self):
        class Broken(FakeRpc):
            def close(self):
                raise RuntimeError("cannot stop codex")

        with self.assertRaisesRegex(RuntimeError, "cannot stop codex"):
            self.take(rpc=Broken(FakeClock(), [delta("Done."), DONE]))


class TelegramVoiceReply(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hubtgv-")
        self.state = os.path.join(self.dir, "state")
        self.env = mock.patch.dict(os.environ, {"HUB_TELEGRAM_CONFIG": os.path.join(self.dir, "telegram.json")})
        self.env.start()
        tg.save_config({"token": "1:x", "owners": [OWNER]})
        self.fake = FakeBotApi()
        self.j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        self.j.append("session.requested", "hub", {"cwd": "/w", "session_id": "h", "kind": "hub"})
        self.j.append("session.started", "hub", {"session_id": "h"})
        self.bot = tg.Bot(self.state, tg.Api("1:x", base=self.fake.base))
        self.bot.voice_sync = True
        self.spoken, self.accounts = [], []
        self.bot.speak = self.fake_speak
        self.bot.catch_up()

    def tearDown(self):
        self.fake.server.shutdown()
        self.fake.server.server_close()
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def fake_speak(self, text, cfg, account=None, session=None):
        self.spoken.append(text)
        self.accounts.append(account)
        d = tempfile.mkdtemp(prefix="hub-speak-")
        with open(os.path.join(d, "reply.ogg"), "wb") as f:
            f.write(b"OggS-voice")
        self.ogg_dir = d
        return os.path.join(d, "reply.ogg"), 12, text

    def owner_says(self, text, mid):
        self.fake.updates = [{"update_id": mid, "message": {"message_id": mid, "from": {"id": OWNER},
                                                            "chat": {"id": OWNER, "type": "private"}, "text": text}}]
        self.bot.poll(timeout=0)
        d = os.path.join(self.state, "spool")
        for _, e in spool.take(d):
            self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"))
        for n in os.listdir(d):
            os.unlink(os.path.join(d, n))

    def answer(self, text="**Done**: finished", who="hub", marker="m3"):
        self.j.append("turn.started", who, {"marker": marker})
        self.j.append("turn.result", who, {"text": text})
        self.bot.tick()
        self.bot.drain_voice()

    def voices(self):
        return self.fake.sent("sendVoice")

    def test_off_by_default_text_only(self):
        self.owner_says("hello", 5)   # m3
        self.answer()
        self.assertEqual(len(self.fake.sent()), 1)
        self.assertEqual((self.voices(), self.spoken), ([], []))

    def test_on_text_then_a_voice_reply_to_his_message(self):
        self.bot.hub_config = lambda: ON
        self.owner_says("what about the mail?", 5)
        self.answer("**Done**: sorted the mail")
        methods = [m for m, _ in self.fake.calls if m in ("sendMessage", "sendVoice")]
        self.assertEqual(methods, ["sendMessage", "sendVoice"])            # text first, voice after
        self.assertEqual(self.spoken, ["**Done**: sorted the mail"])
        raw = self.voices()[0]["raw"]
        self.assertIn(b'name="voice"; filename="reply.ogg"', raw)
        self.assertIn(b"OggS-voice", raw)
        self.assertIn(b'"message_id": 5', raw)                               # a reply to his message
        self.assertIn(b'name="duration"\r\n\r\n12', raw)
        self.assertFalse(os.path.exists(self.ogg_dir))                       # the temporary file is removed
        # a reply to the voice goes to the same addressee as the text
        vid = str(self.fake.next_id)
        self.assertEqual(self.bot.st["routes"].get(vid), {"session": "hub"})

    def test_the_voice_brief_runs_on_the_pool_account(self):
        """2026-10-20: the bot process is not a watchman; the pool account for shortening the reply is from its core state."""
        self.bot.hub_config = lambda: ON
        self.j.append("account.switched", None, {"to": "work", "channel": "core"}, src="core")
        self.owner_says("what about the mail?", 5)
        self.answer("**Done**: sorted the mail", marker="m4")
        self.assertEqual((self.spoken, self.accounts), (["**Done**: sorted the mail"], ["work"]))

    def test_executors_answer_to_his_message_also_gets_a_voice(self):
        self.bot.hub_config = lambda: ON
        self.owner_says("count the revenue", 5)   # m3
        self.j.append("session.requested", "w", {"cwd": "/w", "session_id": "w"})
        self.j.append("session.started", "w", {"session_id": "w"})
        self.j.append("delegation.created", None, {"session": "w", "for": "m3", "channel": "session", "owner": False,
                                                   "sender": "hub"})  # delegation d6, task message m6
        self.answer("42 000 $", who="w", marker="m6")
        self.assertEqual(self.spoken, ["42 000 $"])
        self.assertIn(b'"message_id": 5', self.voices()[0]["raw"])
        self.assertEqual(self.bot.st["routes"].get(str(self.fake.next_id)), {"session": "w", "delegation": "d6"})

    def test_no_voice_without_his_telegram_message_or_for_a_quiet_result(self):
        self.bot.hub_config = lambda: ON
        self.answer("result of the scheduled job", marker=None)   # not a reply to his Telegram message
        self.owner_says("check", 5)
        self.answer("—", marker="m6")                         # a quiet result
        self.assertEqual((self.voices(), self.spoken), ([], []))

    def test_voice_failure_is_a_quiet_line_after_the_text(self):
        self.bot.hub_config = lambda: ON

        def broken(text, cfg, account=None, session=None):
            raise speech.SpeakError("no codex (sign-in with ChatGPT is needed: codex login)")

        self.bot.speak = broken
        self.owner_says("hello", 5)
        self.answer()
        last = self.fake.sent()[-1]
        self.assertEqual(last["text"], "🔇 The voice reply failed: no codex (sign-in with ChatGPT is needed: codex login)")
        self.assertTrue(last["disable_notification"])
        self.assertEqual(self.fake.sent()[0]["text"], "<b>Done</b>: finished")
        self.assertEqual(self.voices(), [])


# --- the reply voice from a module (module-extraction spec, section 3, slice C3; Module API 2.8, speech permission) ---

OGG = b"OggS" + b"\0" * 20


class SpeechMod:
    """A voice module without a process: the answer of the speech.say point passes the real schema check
    (modrun.POINTS), a wrong one is a failure, as in Modules.call. answer(request) -> raw answer; None means silent
    (not ready, timeout)."""

    def __init__(self, answer=None):
        self.answer, self.calls, self.failed, self.up = answer, [], [], True

    def ready(self, name, wanted=None):
        return {"launch": "L1"} if self.up else None

    def call(self, name, point, payload, ctx=None):
        from core import modrun
        self.calls.append((name, point, json.loads(json.dumps(payload))))
        raw = self.answer(payload) if self.answer else None
        if raw is None:
            return None
        try:
            return modrun.POINTS[point]["check"](raw, {"name": name})
        except modrun.BadResult as e:
            self.failed.append(str(e))
            return None


def named(name="vox"):
    return {**ON, "modules": {"use": {"speech": name}}}


class SpeechPoint(unittest.TestCase):
    def check(self, res):
        from core import modrun
        return modrun.POINTS["speech.say"]["check"](res, {"name": "vox"})

    def test_the_point_belongs_to_2_8_and_needs_the_speech_right(self):
        from core import modrun, modules, version
        self.assertIn("speech.say", version.MODULE_POINTS["2.8"])
        self.assertEqual(version.manifest_since("speech"), "2.8")
        self.assertIn("speech", modules.CAPABILITIES)
        self.assertEqual((modrun.POINTS["speech.say"]["cap"], modrun.POINTS["speech.say"]["budget"]), ("speech", 150.0))
        hello = {"name": "vox", "version": "1.0.0", "points": ["speech.say"]}
        ctx = {"name": "vox", "version": "1.0.0", "api": "^2.8"}
        with self.assertRaisesRegex(modrun.BadResult, "needs the capability speech"):
            modrun.POINTS["hello"]["check"](hello, {**ctx, "capabilities": []})
        with self.assertRaisesRegex(modrun.BadResult, "module API 2.8"):
            modrun.POINTS["hello"]["check"](hello, {**ctx, "capabilities": ["speech"], "api": "^2.7"})
        self.assertEqual(modrun.POINTS["hello"]["check"](hello, {**ctx, "capabilities": ["speech"]})["points"],
                         ["speech.say"])

    def test_answers_by_the_schema(self):
        ogg = os.path.abspath("/x/r.ogg")   # absolute on this OS: on Windows (Python 3.13+) "/x/r.ogg" is not, C:\x\r.ogg is
        self.assertEqual(self.check({"path": ogg, "seconds": 12.5, "said": "Done."}),
                         {"path": ogg, "seconds": 12.5, "said": "Done."})
        self.assertEqual(self.check({"error": "no codex"}), {"error": "no codex"})
        self.assertEqual(self.check({}), {})   # "nothing": no voice, the text is already with the owner

    def test_a_wrong_answer_is_a_failure(self):
        from core import modrun
        for bad in (None, [], "ok", {"path": "r.ogg", "seconds": 5, "said": "x"},   # the path is not absolute
                    {"path": "/x/r.ogg", "seconds": 0, "said": "x"}, {"path": "/x/r.ogg", "seconds": True, "said": "x"},
                    {"path": "/x/r.ogg", "seconds": 5}, {"path": "/x/r.ogg", "seconds": 5, "said": 3},
                    {"error": ""}, {"error": 5}, {"error": "x", "path": "/x/r.ogg", "seconds": 5, "said": "x"},
                    {"path": "/x/r.ogg", "seconds": 5, "said": "x", "extra": 1}):
            with self.subTest(bad=bad), self.assertRaises(modrun.BadResult):
                self.check(bad)


class Facade(unittest.TestCase):
    """modules/voice/speech.py: who speaks, the built-in speak.py or the module named in modules.use.speech."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hub-sf-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def ogg(self, body=OGG, name="r.ogg"):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as f:
            f.write(body)
        return path

    def facade(self, answer):
        mod = SpeechMod(answer)
        return speech.Speech(self.dir, mods=mod), mod

    @cut.needs("speech")
    def test_without_a_named_module_the_builtin_voice_speaks_and_the_module_is_not_asked(self):
        f, mod = self.facade(lambda q: {"error": "must not sound"})
        built = []
        with mock.patch.object(speak, "reply", lambda text, cfg, account=None: built.append((text, account)) or "b"):
            for cfg in (ON, {**ON, "modules": {"use": {"speech": "builtin"}}}, {**ON, "modules": {"use": {"speech": 5}}}):
                self.assertEqual(f.reply("Hello", cfg, account="work", session="maps"), "b")
        self.assertEqual((built, mod.calls), ([("Hello", "work")] * 3, []))

    def test_the_named_module_gets_text_channel_and_session_and_its_file_is_copied(self):
        path = self.ogg()
        f, mod = self.facade(lambda q: {"path": path, "seconds": 11.6, "said": "Hello."})
        got = f.reply("**Hello**", named(), account="work", session="maps")
        self.assertEqual(mod.calls, [("vox", "speech.say", {"text": "**Hello**", "channel": "telegram",
                                                           "session": "maps"})])
        ogg, seconds, said = got
        self.assertEqual((seconds, said), (12, "Hello."))
        self.assertNotEqual(ogg, path)                           # the bot removes its copy, not the module's file
        with open(ogg, "rb") as fh:
            self.assertEqual(fh.read(), OGG)
        self.assertTrue(os.path.isfile(path))
        shutil.rmtree(os.path.dirname(ogg))

    @cut.needs("speech")
    def test_the_builtin_is_never_used_instead_of_a_named_module(self):
        f, mod = self.facade(None)
        with mock.patch.object(speak, "reply", side_effect=AssertionError("the module must voice it")):
            self.assertIsNone(f.reply("Hello", named()))        # silent (not ready, timeout): the reply stays as text
        self.assertEqual(len(mod.calls), 1)

    def test_nothing_is_no_voice_and_an_error_is_a_failure_with_its_text(self):
        f, mod = self.facade(lambda q: {})
        self.assertIsNone(f.reply("Hello", named()))
        f, mod = self.facade(lambda q: {"error": "no Chrome"})
        with self.assertRaisesRegex(speech.SpeakError, "^no Chrome$"):
            f.reply("Hello", named())

    def test_a_file_that_is_not_a_voice_message_is_never_sent(self):
        # the module names the path: a foreign file (keys, settings) does not go to Telegram, and the file's folder is
        # not deleted
        before = set(os.listdir(tempfile.gettempdir()))
        for name, body in (("secret.txt", b"password=1\n"), ("empty.ogg", b"")):
            path = self.ogg(body, name)
            f, _ = self.facade(lambda q, p=path: {"path": p, "seconds": 5, "said": "x"})
            with self.subTest(name), self.assertRaisesRegex(speech.SpeakError, "not a voice message"):
                f.reply("Hello", named())
            self.assertTrue(os.path.isfile(path))
        link = os.path.join(self.dir, "link.ogg")                # a link to a non-voice file: judged by content, like a file
        odd = {"directory": self.dir, "null byte": self.ogg() + "\0.ogg"}
        if winsafe.try_symlink(self.ogg(b"password=1\n", "k.txt"), link):   # (Windows without the right: no such case)
            odd["link"] = link
        if hasattr(os, "mkfifo"):
            odd["pipe"] = os.path.join(self.dir, "pipe.ogg")     # opening it the usual way would hang the voicing
            os.mkfifo(odd["pipe"])
        for what, path in odd.items():
            f, _ = self.facade(lambda q, p=path: {"path": p, "seconds": 5, "said": "x"})
            with self.subTest(what), self.assertRaisesRegex(speech.SpeakError, "not a voice message"):
                f.reply("Hello", named())
        f, _ = self.facade(lambda q: {"path": os.path.join(self.dir, "missing.ogg"), "seconds": 5, "said": "x"})
        with self.assertRaisesRegex(speech.SpeakError, "not a voice message"):
            f.reply("Hello", named())
        f, _ = self.facade(lambda q: {"path": self.ogg(), "seconds": 5, "said": "x"})
        with mock.patch.object(speech, "MAX_BYTES", 10), self.assertRaisesRegex(speech.SpeakError, "not a voice message"):
            f.reply("Hello", named())
        self.assertEqual({n for n in os.listdir(tempfile.gettempdir()) if n.startswith("hub-speak-")} - before, set())

    def test_a_wrong_schema_answer_is_silence_and_enabled_is_the_same_switch(self):
        f, mod = self.facade(lambda q: {"path": "r.ogg", "seconds": 5, "said": "x"})
        self.assertIsNone(f.reply("Hello", named()))
        self.assertEqual(len(mod.failed), 1)
        self.assertFalse(speech.enabled({"modules": {"use": {"speech": "vox"}}}))   # the switch is voice_reply.enabled
        self.assertTrue(speech.enabled(named()))


class TelegramModuleVoice(TelegramVoiceReply):
    """The same bot, but the voice goes through the facade and a fake module (the bot's own `speak` is not replaced)."""

    def setUp(self):
        super().setUp()
        self.bot.hub_config = lambda: named()
        self.asked, self.path = [], os.path.join(self.dir, "mod-reply.ogg")
        with open(self.path, "wb") as f:
            f.write(OGG)
        self.answer_with = lambda q: {"path": self.path, "seconds": 12, "said": "Done"}
        self.mod = SpeechMod(lambda q: self.asked.append(q) or self.answer_with(q))
        self.bot.speech.mods = self.mod
        self.bot.speak = self.bot.speech.reply

    # the bot's inherited tests replace the voice and settings in their own way; not repeated here
    test_off_by_default_text_only = test_on_text_then_a_voice_reply_to_his_message = None
    test_the_voice_brief_runs_on_the_pool_account = test_executors_answer_to_his_message_also_gets_a_voice = None
    test_no_voice_without_his_telegram_message_or_for_a_quiet_result = None
    test_voice_failure_is_a_quiet_line_after_the_text = None

    def test_text_first_then_the_voice_of_the_module_as_a_reply_once(self):
        self.owner_says("what about the mail?", 5)
        self.answer("**Done**: sorted the mail")
        self.assertEqual([m for m, _ in self.fake.calls if m in ("sendMessage", "sendVoice")],
                         ["sendMessage", "sendVoice"])
        self.assertEqual(self.asked, [{"text": "**Done**: sorted the mail", "channel": "telegram", "session": "hub"}])
        raw = self.voices()[0]["raw"]
        self.assertIn(b"OggS", raw)
        self.assertIn(b'"message_id": 5', raw)
        self.assertIn(b'name="duration"\r\n\r\n12', raw)
        self.assertTrue(os.path.isfile(self.path))                      # the bot does not remove the module's file
        self.assertEqual([n for n in os.listdir(self.dir) if n.startswith("hub-speak-")], [])
        self.assertEqual(self.bot.st["routes"].get(str(self.fake.next_id)), {"session": "hub"})

    def test_off_means_the_module_is_not_even_asked(self):
        self.bot.hub_config = lambda: {"modules": {"use": {"speech": "vox"}}}
        self.owner_says("hello", 5)
        self.answer()
        self.assertEqual((self.voices(), self.asked, len(self.fake.sent())), ([], [], 1))

    def test_a_silent_module_leaves_the_text_alone(self):
        self.answer_with = lambda q: {}
        self.owner_says("hello", 5)
        self.answer()
        self.assertEqual(([m["text"] for m in self.fake.sent()], self.voices()), (["<b>Done</b>: finished"], []))

    def test_an_error_of_the_module_is_the_quiet_line_after_the_text(self):
        self.answer_with = lambda q: {"error": "no codex"}
        self.owner_says("hello", 5)
        self.answer()
        self.assertEqual(self.fake.sent()[-1]["text"], "🔇 The voice reply failed: no codex")
        self.assertEqual(self.voices(), [])


@cut.needs("speech")
class TelegramVoiceProvider(TelegramModuleVoice):
    """The voice is run by a module the owner did not name: hubd chose it (core/providers.py, st["providers"]["speech"]).
    Every voicing by a module is a speech.said fact; two failures in a row: the next replies are voiced by the built-in
    one; a module of this version is in hold. "No voice" ({}) is not a failure. One voice message per reply."""

    test_text_first_then_the_voice_of_the_module_as_a_reply_once = test_off_means_the_module_is_not_even_asked = None
    test_a_silent_module_leaves_the_text_alone = test_an_error_of_the_module_is_the_quiet_line_after_the_text = None

    def setUp(self):
        super().setUp()
        self.bot.hub_config = lambda: ON
        self.j.append("module.ready", "vox", {"module": "vox", "launch": "L1", "version": "1.0.0",
                                              "points": ["speech.say"], "channel": "core"})
        self.j.append("module.provider", None, {"cap": "speech", "state": "module", "module": "vox", "why": "ready",
                                                "channel": "core"})
        self.builtin = []
        p = mock.patch.object(speak, "reply", lambda text, cfg, account=None: self.builtin.append(text) or
                              self.fake_speak(text, cfg))
        p.start()
        self.addCleanup(p.stop)

    def flush(self):
        d = os.path.join(self.state, "spool")
        for path, e in spool.take(d):
            self.j.append(e["type"], e.get("entity"), e.get("data"), src=e.get("src"))
            os.unlink(path)
        self.bot.tick()

    def reply(self, mid):
        self.owner_says("what about the mail?", mid)
        seq = [e["seq"] for e in journal.read_tail(os.path.join(self.state, "journal.jsonl"))
               if e["type"] == "message.received"][-1]
        self.answer("**Done**: sorted the mail", marker=f"m{seq}")
        self.flush()

    def said(self):
        return [e["data"]["ok"] for e in journal.read_tail(os.path.join(self.state, "journal.jsonl"))
                if e["type"] == "speech.said"]

    def provider(self):
        return self.bot.reader.state["providers"]["speech"]

    def test_the_module_chosen_by_the_hub_speaks_and_its_first_voice_proves_it(self):
        self.reply(5)
        self.assertEqual((len(self.asked), len(self.voices()), self.builtin), (1, 1, []))
        self.assertEqual(self.said(), [True])
        self.assertTrue(self.provider().get("proved"))

    def test_two_failures_in_a_row_hand_the_voice_back_and_one_success_resets(self):
        self.answer_with = lambda q: None            # did not answer
        self.reply(5)
        self.answer_with = lambda q: {"path": self.path, "seconds": 12, "said": "Done"}
        self.reply(7)
        self.answer_with = lambda q: {"error": "no codex"}
        self.reply(9)
        self.assertEqual(self.provider()["state"], "module")   # a success between failures reset the count
        self.reply(11)
        self.assertEqual(self.said(), [False, True, False, False])
        self.assertEqual((self.provider()["state"], self.provider()["hold"]["version"]), ("builtin", "1.0.0"))
        asked = len(self.asked)
        self.reply(13)
        self.assertEqual((len(self.asked), self.builtin), (asked, ["**Done**: sorted the mail"]))
        self.assertEqual(self.said()[-1:], [False])            # the built-in voice is not marked by facts

    def test_nothing_to_say_is_not_a_failure(self):
        self.answer_with = lambda q: {}
        for mid in (5, 7, 9):
            self.reply(mid)
        self.assertEqual((self.said(), self.provider()["state"], self.voices()), ([], "module", []))

    def test_one_voice_per_answer_and_each_answer_to_the_same_message_has_its_own(self):
        self.reply(5)
        key = next(iter(self.bot.st["voiced"]))
        got = tempfile.mkdtemp(prefix="hub-speak-")
        with open(os.path.join(got, "reply.ogg"), "wb") as f:
            f.write(OGG)
        self.bot.send_voice(5, {"session": "hub"}, (os.path.join(got, "reply.ogg"), 3, "x"), None, "vox", key)
        self.assertEqual(len(self.voices()), 1)                      # the same reply: no second voice message
        seq = [e["seq"] for e in journal.read_tail(os.path.join(self.state, "journal.jsonl"))
               if e["type"] == "message.received"][-1]
        self.answer("**Done**: and one more", marker=f"m{seq}")      # a second reply to the same message
        self.assertEqual(len(self.voices()), 2)

    def test_a_module_not_up_yet_is_not_a_failure(self):
        self.mod.up = False                                          # hub restart: no hello yet
        for mid in (5, 7, 9):
            self.reply(mid)
        self.assertEqual((self.said(), self.provider()["state"], self.asked), ([], "module", []))


class ModuleProcess(unittest.TestCase):
    """speech.say through a real module process (tests/fakes/fake_module.py, started as hub-up does): hello with the
    speech permission and api ^2.8, answers over HTTP; a module that went silent or answered off-schema is a failure
    in the journal, no voice."""

    @cut.needs("speech")
    def test_the_point_over_http(self):
        from tests.fakes.modstand import Stand
        s = Stand(self, points=["speech.say"], caps=["speech"])
        path = os.path.join(s.root, "fake", "module.json")
        with open(path) as f:
            man = json.load(f)
        with open(path, "w") as f:
            json.dump({**man, "api": "^2.8"}, f)
        s.hub.modules.reload(s.cfg, {})
        ogg = os.path.join(s.state_dir, "said.ogg")
        with open(ogg, "wb") as f:
            f.write(OGG)
        s.update(**{"speech.say": {"do": "ok", "answer": {"path": ogg, "seconds": 9, "said": "Done."}}})
        s.launch()
        s.until_ready()
        self.assertEqual(s.rec()["points"], ["speech.say"])
        f = speech.Speech(s.state_dir, mods=s.hub.modules.mods)
        cfg = named("fake")
        got = f.reply("**Done**", cfg, session="maps")
        self.assertEqual((got[1], got[2]), (9, "Done."))
        shutil.rmtree(os.path.dirname(got[0]))
        self.assertEqual([c["body"] for c in s.calls("speech.say")],
                         [{"text": "**Done**", "channel": "telegram", "session": "maps"}])
        s.update(**{"speech.say": {"do": "ok", "answer": {"error": "no Chrome"}}})
        with self.assertRaisesRegex(speech.SpeakError, "no Chrome"):
            f.reply("Hello", cfg)
        s.update(**{"speech.say": {"do": "garbage"}})
        self.assertIsNone(f.reply("Hello", cfg))
        self.assertEqual([e["data"]["hook"] for e in s.events("module.error")], ["speech.say"])
        # another module or none is named: this one is not called
        n = len(s.calls("speech.say"))
        with mock.patch.object(speak, "reply", lambda text, cfg, account=None: "builtin"):
            self.assertEqual(f.reply("Hello", ON), "builtin")
        self.assertEqual(len(s.calls("speech.say")), n)


class ModulesToggle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-vr-")
        self.cfg = os.path.join(self.tmp, "hub.json")
        with open(self.cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "voice_reply": {"voice": "cove"}}, f)
        self.env = mock.patch.dict(os.environ, {"HUB_CONFIG": self.cfg})
        self.env.start()
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()

    def tearDown(self):
        self.h.tearDown()
        self.env.stop()

    def mod(self):
        return {m["id"]: m for m in json.loads(self.h.req("GET", "/api/modules")[1])["modules"]}["voice_reply"]

    def test_card_with_a_switch_off_by_default_and_the_switch_writes_hub_json(self):
        self.h.seed()
        m = self.mod()
        self.assertEqual((m["state"], m["toggle"]), ("off", True))
        self.assertGreater(len(m["gives"]), 40)
        code, out = self.h.req("POST", "/api/modules/toggle", {"id": "voice_reply", "enabled": True})
        self.assertEqual(code, 200, out)
        with open(self.cfg) as f:
            saved = json.load(f)
        self.assertEqual(saved["voice_reply"], {"voice": "cove", "enabled": True})   # the section's own values are not lost
        self.assertEqual(saved["dev"], {"enabled": False})
        self.assertIn(self.mod()["state"], ("on", "part"))
        self.h.req("POST", "/api/modules/toggle", {"id": "voice_reply", "enabled": False})
        self.assertEqual(self.mod()["state"], "off")

    def test_only_switchable_modules(self):
        self.h.seed()
        code, _ = self.h.req("POST", "/api/modules/toggle", {"id": "files", "enabled": True})
        self.assertEqual(code, 400)
        js = self.h.req("GET", "/static/js/modules.js")[1].decode()
        self.assertIn("/api/modules/toggle", js)


if __name__ == "__main__":
    unittest.main()
