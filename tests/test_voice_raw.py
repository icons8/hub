"""A recording as a raw stream (`POST /api/voice/raw`): the body is the sound itself, not base64 in JSON; one upload
session per recording (chunks go to the same session by offset, a retry of a chunk is safe); the old base64 route stays."""
import tests  # noqa: F401 — do not see the machine's settings, see tests/__init__.py
import json
import os
import socket
import tempfile
import time
import threading
import unittest
from unittest import mock

import platforms
from modules.voice import transcribe as voice
from modules.web.server import MAX_FILE, Web


class VoiceRawTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubvraw-")
        self.cfg = {"voice": {"cmd": "asr {wav}", "ffmpeg": "ffmpeg"}}
        self.web = Web(self.state)
        self.web.config = lambda: self.cfg
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.clock = [time.time()]
        self.web.voice_clock = lambda: self.clock[0]
        self.heard = []
        p = mock.patch.object(voice, "transcribe", lambda raw, cfg, suffix, **kw: self.heard.append((raw, suffix)) or "hello")
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        platforms.current.rmtree(self.state)

    def start(self, query, body, first=None, path="/api/voice/raw"):
        """A request whose body is sent only up to `first` bytes: the rest goes with finish() (a slow client)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        s.sendall((f"POST {path}{query} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n"
                   f"Content-Length: {len(body)}\r\n\r\n").encode() + body[:first])
        return s, body[first:] if first is not None else b""

    def post(self, query, body=b"", path="/api/voice/raw"):
        return self.finish(*self.start(query, body, path=path))

    def finish(self, s, rest_of_body=b""):
        if rest_of_body:
            s.sendall(rest_of_body)
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.recv(s)
        top, _, rest = data.partition(b"\r\n\r\n")
        n = int(next(ln.split(b":", 1)[1] for ln in top.split(b"\r\n") if ln.lower().startswith(b"content-length:")))
        while len(rest) < n:
            rest += self.recv(s)
        self.last_headers = top.lower()
        s.close()
        return int(top.split(b" ")[1]), json.loads(rest or b"{}")

    def recv(self, s):
        got = s.recv(65536)
        if not got:   # the hub closed the connection: without this the loops above would spin forever
            s.close()
            self.fail("the connection was closed with no answer")
        return got

    def test_one_request_is_the_whole_recording(self):
        code, out = self.post("?mime=audio/webm", b"RIFFsound")
        self.assertEqual((code, out), (200, {"text": "hello"}))
        self.assertEqual(self.heard, [(b"RIFFsound", ".webm")])

    def test_chunks_join_one_session_by_offset(self):
        code, out = self.post("?mime=audio/webm&done=0", b"abc")
        self.assertEqual((code, out["size"]), (200, 3))
        sid = out["id"]
        self.assertEqual(self.post(f"?id={sid}&offset=3&done=0", b"def")[1]["size"], 6)
        # a retried chunk (the answer was lost) does not double the sound; a gap is refused with the size to resume from
        self.assertEqual(self.post(f"?id={sid}&offset=3&done=0", b"def")[1]["size"], 6)
        code, out = self.post(f"?id={sid}&offset=9&done=0", b"zzz")
        self.assertEqual((code, out["size"]), (409, 6))
        code, out = self.post(f"?id={sid}&offset=6&done=1", b"g")
        self.assertEqual((code, out), (200, {"text": "hello"}))
        self.assertEqual(self.heard, [(b"abcdefg", ".webm")])
        self.assertEqual(self.post(f"?id={sid}&offset=6&done=1", b"g"), (200, {"text": "hello"}))   # the answer got lost: the same one
        self.assertEqual(len(self.heard), 1)
        self.clock[0] += self.web.VOICE_ANSWER_TTL + 1
        self.assertEqual(self.post(f"?id={sid}&offset=7", b"x")[0], 404)   # the session is gone for good

    def left(self):
        import os
        d = os.path.join(self.state, "voice-upload")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def test_a_client_that_leaves_mid_body_leaves_nothing_and_the_session_stays_usable(self):
        sid = self.post("?done=0", b"abc")[1]["id"]
        for q in ("?done=0", f"?id={sid}&offset=3&done=0"):
            s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
            s.sendall((f"POST /api/voice/raw{q} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-Hub: 1\r\n"
                       "Content-Length: 100\r\n\r\n").encode() + b"half")
            s.close()
        deadline = time.monotonic() + 10   # the server drops the dropped chunk as soon as it sees the end of the connection
        while time.monotonic() < deadline and any(n != sid + ".part" for n in self.left()):
            time.sleep(0.05)
        self.assertEqual(self.left(), [sid + ".part"])   # no remnant of the dropped chunks, the session holds only 3 bytes
        self.assertEqual(self.post(f"?id={sid}&offset=3&done=1", b"d")[0], 200)
        self.assertEqual(self.heard, [(b"abcd", ".webm")])
        self.assertEqual(self.left(), [])

    def test_a_session_nobody_finishes_is_dropped_after_the_ttl(self):
        stale = self.post("?done=0", b"abc")[1]["id"]
        self.clock[0] += self.web.VOICE_SESSION_TTL + 10
        fresh = self.post("?done=0", b"x")[1]["id"]
        self.assertEqual(self.left(), [fresh + ".part"])
        self.assertEqual(self.post(f"?id={stale}&offset=3", b"d")[0], 404)

    def test_a_chunk_retried_while_the_first_try_is_slow_does_not_double_the_sound(self):
        sid = self.post("?done=0", b"abc")[1]["id"]
        slow, rest = self.start(f"?id={sid}&offset=3&done=0", b"def", first=1)   # the first try: one byte, then silence
        self.assertEqual(self.post(f"?id={sid}&offset=3&done=0", b"def")[1]["size"], 6)   # the retry overtakes it
        self.assertEqual(self.finish(slow, rest)[1]["size"], 6)   # the late one finds it written
        self.assertEqual(self.post(f"?id={sid}&offset=6", b"g")[0], 200)
        self.assertEqual(self.heard, [(b"abcdefg", ".webm")])

    def test_the_open_sessions_are_capped_and_the_oldest_go_first(self):
        ids = []
        for i in range(self.web.VOICE_SESSIONS_MAX + 3):
            self.clock[0] += 1
            ids.append(self.post("?done=0", b"x")[1]["id"])
            os.utime(os.path.join(self.state, "voice-upload", ids[-1] + ".part"), (self.clock[0], self.clock[0]))
        self.assertEqual(self.left(), sorted(i + ".part" for i in ids[3:]))
        self.assertEqual(self.post(f"?id={ids[0]}&offset=1&done=0", b"y")[0], 404)

    def test_a_recording_in_one_request_does_not_push_an_open_session_out(self):
        ids = []
        for i in range(self.web.VOICE_SESSIONS_MAX):
            self.clock[0] += 1
            ids.append(self.post("?done=0", b"x")[1]["id"])
            os.utime(os.path.join(self.state, "voice-upload", ids[-1] + ".part"), (self.clock[0], self.clock[0]))
        self.assertEqual(self.post("?mime=audio/webm", b"whole")[0], 200)   # it needs no place among the open sessions
        self.assertEqual(self.post(f"?id={ids[0]}&offset=1&done=0", b"y")[0], 200)
        self.assertEqual(len(self.left()), self.web.VOICE_SESSIONS_MAX)

    def test_a_chunk_that_does_not_end_the_recording_is_refused_once_it_has_ended(self):
        sid = self.post("?done=0", b"abc")[1]["id"]
        self.assertEqual(self.post(f"?id={sid}&offset=3", b"def"), (200, {"text": "hello"}))
        code, out = self.post(f"?id={sid}&offset=3&done=0", b"def")   # it expects {id, size}, not the text
        self.assertEqual(code, 404, out)
        self.assertEqual(self.post(f"?id={sid}&offset=3", b"def"), (200, {"text": "hello"}))   # the last one again: the answer
        self.assertEqual(len(self.heard), 1)

    def test_the_last_chunk_retried_while_it_is_transcribing_gets_the_answer_not_a_404(self):
        entered, release = threading.Event(), threading.Event()

        def slow(raw, cfg, suffix, **kw):
            self.heard.append((raw, suffix))
            entered.set()
            release.wait(30)
            return "hello"
        sid = self.post("?done=0", b"ab")[1]["id"]
        with mock.patch.object(voice, "transcribe", slow):
            first = []
            t = threading.Thread(target=lambda: first.append(self.post(f"?id={sid}&offset=2", b"c")))
            t.start()
            self.assertTrue(entered.wait(30))
            again = []
            t2 = threading.Thread(target=lambda: again.append(self.post(f"?id={sid}&offset=2", b"c")))
            t2.start()
            self.assertTrue(self.web.voice_answers[sid]["event"].wait(0.3) is False)   # still transcribing
            release.set()
            t.join(30)
            t2.join(30)
        self.assertEqual((first, again), ([(200, {"text": "hello"})], [(200, {"text": "hello"})]))
        self.assertEqual(self.heard, [(b"abc", ".webm")])   # transcribed once

    def test_a_refusal_with_a_slow_body_does_not_hold_the_lock_for_other_sessions(self):
        import select
        from modules.web import modproxy
        draining, go_on, drain = threading.Event(), threading.Event(), modproxy.drain

        def held(*a, **kw):   # the refusal reads its body off: it waits here until the other recording is answered
            draining.set()
            go_on.wait(10)
            return drain(*a, **kw)
        code, out = self.post("?done=0", b"abc")
        sid = out["id"]
        with mock.patch.object(modproxy, "drain", held):
            s, rest = self.start(f"?id={sid}&offset=9&done=0", b"z" * 100, first=10)   # a gap: refused, its body still on the way
            self.assertTrue(draining.wait(30))
            code, out = self.post("?mime=audio/webm", b"other")   # another recording meanwhile
            self.assertEqual(code, 200)
            # it was answered while the refused body is still being read off; with the drain under the lock the other
            # recording waits for it, and by then the 409 is already written
            self.assertEqual(select.select([s], [], [], 0)[0], [])
            go_on.set()
            self.assertEqual(self.finish(s, rest)[0], 409)

    def test_mime_and_session_of_the_first_chunk_stay_when_the_last_one_has_none(self):
        # not audio/webm: .webm is also the suffix with no type, so a lost mime would pass unseen
        sid = self.post("?mime=audio/mp4&session=s1&done=0", b"abc")[1]["id"]
        self.assertEqual(self.post(f"?id={sid}&offset=3", b"def")[0], 200)
        self.assertEqual(self.heard, [(b"abcdef", ".m4a")])
        seen, context = [], self.web.voice_context
        self.web.voice_context = lambda session: seen.append(session) or context(session)

        def fails(raw, cfg, suffix, **kw):
            raise voice.VoiceError("no")
        sid = self.post("?mime=audio/mp4&session=s1&done=0", b"abc")[1]["id"]
        with mock.patch.object(voice, "transcribe", fails), mock.patch("sys.stderr"):
            self.assertEqual(self.post(f"?id={sid}&offset=3", b"def")[0], 502)
        self.assertEqual(seen, ["s1"])   # the failed recording says where the owner was speaking

    def test_any_error_of_the_transcription_keeps_the_recording(self):
        def boom(raw, cfg, suffix, **kw):
            raise RuntimeError("engine broke")
        self.web.voice_context = lambda session: f"to {session}"
        with mock.patch.object(voice, "transcribe", boom), mock.patch("sys.stderr"):
            code, out = self.post("?mime=audio/webm&session=s1", b"RIFFsound")
        self.assertEqual(code, 502)
        self.assertIn("id", out)
        self.assertEqual(out["context"], "to s1")   # as a voice error's 502 has it
        kept = os.path.join(self.state, "voice-failed")
        self.assertEqual([open(os.path.join(kept, n), "rb").read() for n in os.listdir(kept)], [b"RIFFsound"])

    def test_a_failed_recording_that_cannot_be_kept_either_is_still_answered_and_so_is_its_retry(self):
        def boom(raw, cfg, suffix, **kw):
            raise RuntimeError("engine broke")
        sid = self.post("?done=0", b"abc")[1]["id"]
        with mock.patch.object(voice, "transcribe", boom), mock.patch("sys.stderr"), \
                mock.patch.object(self.web, "keep_failed_voice", side_effect=OSError(28, "No space left on device")):
            code, out = self.post(f"?id={sid}&offset=3", b"def")
            self.assertEqual(code, 500)
            self.assertIn("No space", out["error"])
            self.assertEqual(self.post(f"?id={sid}&offset=3", b"def"), (code, out))   # the retry gets it, not a 300 s wait

    def test_a_chunk_of_a_session_without_offset_is_a_400(self):
        # appended at the size the hub has, a repeat (its answer lost) would double the sound
        sid = self.post("?done=0", b"abc")[1]["id"]
        self.assertEqual(self.post(f"?id={sid}&done=0", b"def")[0], 400)
        self.assertEqual(self.post(f"?id={sid}&offset=3", b"def")[0], 200)
        self.assertEqual(self.heard, [(b"abcdef", ".webm")])

    def test_a_chunk_that_would_pass_the_limit_is_refused_before_its_body(self):
        from modules.web import modproxy
        sid = self.post("?done=0", b"abc")[1]["id"]
        s, _ = self.start(f"?id={sid}&offset=3&done=0", b"z" * (MAX_FILE - 2), first=0)   # the body is never sent
        with mock.patch.object(modproxy, "drain", lambda *a, **kw: None):   # not the 5 s wait for a body that never comes
            code, out = self.finish(s)
        self.assertEqual((code, b"connection: close" in self.last_headers), (413, True))
        self.assertEqual(self.left(), [])   # the session cannot end under the limit any more

    def test_the_id_of_a_kept_recording_is_answered_even_if_its_context_cannot_be_read(self):
        def broken(session):
            raise KeyError(session)
        self.web.voice_context = broken
        for fails in (RuntimeError("engine broke"), voice.VoiceError("no")):
            def boom(raw, cfg, suffix, **kw):
                raise fails
            with mock.patch.object(voice, "transcribe", boom), mock.patch("sys.stderr"):
                code, out = self.post("?mime=audio/webm&session=s1", b"RIFFsound")
            self.assertEqual((code, "id" in out, "context" in out), (502, True, False), fails)

    def test_a_malformed_id_without_offset_is_a_404_not_a_400(self):
        self.assertEqual(self.post("?id=nope&done=0", b"x")[0], 404)

    def test_a_bad_offset_is_a_400_and_the_connection_closes_with_the_body_unread(self):
        for off in ("%C2%B2", "1.5", "-1", "9" * 30):   # "²" is a digit for str.isdigit()
            code, out = self.post(f"?offset={off}", b"abc")
            self.assertEqual(code, 400, off)
            self.assertIn("error", out)
            self.assertIn(b"connection: close", self.last_headers)
        self.assertEqual(self.left(), [])

    def test_a_refusal_before_the_body_closes_the_connection(self):
        code, _ = self.post("?id=0123456789abcdef&offset=0", b"abc")
        self.assertEqual((code, b"connection: close" in self.last_headers), (404, True))
        sid = self.post("?done=0", b"abc")[1]["id"]
        code, out = self.post(f"?id={sid}&offset=9", b"abc")
        self.assertEqual((code, out["size"], b"connection: close" in self.last_headers), (409, 3, True))

    def test_a_disk_error_is_answered_not_dropped(self):
        with mock.patch("modules.web.server.os.open", side_effect=OSError(28, "No space left on device")):
            code, out = self.post("?done=0", b"abc")
        self.assertEqual(code, 500)
        self.assertIn("No space", out["error"])

    def test_the_session_files_are_for_the_owner_only(self):
        import stat
        self.post("?done=0", b"abc")
        folder = os.path.join(self.state, "voice-upload")
        name = os.listdir(folder)[0]
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(folder, name)).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(folder).st_mode), 0o700)

    def test_unknown_session_and_empty_and_off(self):
        self.assertEqual(self.post("?id=nope&offset=0", b"x")[0], 404)
        self.assertEqual(self.post("", b"")[0], 400)
        self.cfg = {}
        self.assertEqual(self.post("", b"x")[0], 404)

    def test_the_old_base64_route_stays_and_the_feature_is_named(self):
        import base64
        self.assertEqual(self.web.act("/api/voice", {"data": base64.b64encode(b"rec").decode()})[0], 200)
        self.assertIn("voice.raw", self.web.with_clock({})["features"] if hasattr(self.web, "with_clock") else [])

    def test_the_snapshot_asks_whether_voice_is_on_once(self):
        calls, enabled = [], voice.enabled
        with mock.patch.object(voice, "enabled", lambda *a, **kw: calls.append(1) or enabled(*a, **kw)):
            snap = self.web.with_clock({})
        self.assertEqual((snap["voice"], "voice.raw" in snap["features"], len(calls)), (True, True, 1))


if __name__ == "__main__":
    unittest.main()
