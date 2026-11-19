"""An attachment as a raw stream (`POST /api/upload/raw`): the body is the file itself, written to disk in chunks,
without reading it whole into memory; only with Content-Length, over the limit — 413, a client disconnect — no
half-written file is left."""
import tests  # noqa: F401 — do not see the machine's settings (~/.config/hub-core), see tests/__init__.py
import hashlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import tracemalloc
import unittest
from urllib.parse import quote

from modules.web.server import Web

MB = 1 << 20


class UploadRawTest(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="hubraw-")
        self.cfg = {}
        self.web = Web(self.state)
        self.web.config = lambda: self.cfg
        self.server = self.web.serve(port=0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.web.stop.set()
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.state, ignore_errors=True)

    def files(self):
        root = os.path.join(self.state, "attachments")
        return sorted(os.path.relpath(os.path.join(d, f), root) for d, _, fs in os.walk(root) for f in fs) \
            if os.path.isdir(root) else []

    def dirs(self):
        root = os.path.join(self.state, "attachments")
        return os.listdir(root) if os.path.isdir(root) else []

    def raw(self, query, body=b"", length=None, headers=None, send=None):
        """A hand-made request: own headers (no Content-Length if length=False), the body is body or send(sock)."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        h = {"Host": f"127.0.0.1:{self.port}", "X-Hub": "1", "Content-Type": "application/octet-stream",
             **({} if length is False else {"Content-Length": str(len(body) if length is None else length)}),
             **(headers or {})}
        head = f"POST /api/upload/raw{query} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items() if v is not None)
        s.sendall(head.encode("latin-1") + b"\r\n")
        try:
            if send:
                send(s)
            elif body:
                s.sendall(body)
        except OSError:
            pass
        data = b""
        try:
            while b"\r\n\r\n" not in data:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
            top, _, rest = data.partition(b"\r\n\r\n")
            n = int(next((ln.split(b":", 1)[1] for ln in top.split(b"\r\n") if ln.lower().startswith(b"content-length:")), 0))
            while len(rest) < n:
                chunk = s.recv(65536)
                if not chunk:
                    break
                rest += chunk
        finally:
            s.close()
        code = int(top.split(b" ")[1]) if top else 0
        return code, json.loads(rest or b"{}")

    def test_a_200_mb_file_streams_to_disk_without_growing_memory(self):
        block = os.urandom(MB)
        want = hashlib.sha256()
        for _ in range(200):
            want.update(block)

        def send(s):
            for _ in range(200):
                s.sendall(block)
        tracemalloc.start()
        try:
            base = tracemalloc.get_traced_memory()[0]
            tracemalloc.reset_peak()
            code, meta = self.raw("?name=big.bin&mime=application/zip", length=200 * MB, send=send)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(code, 200, meta)
        self.assertLess(peak - base, 8 * MB)   # in chunks: the body never enters memory whole
        self.assertEqual((meta["name"], meta["mime"], meta["size"]), ("big.bin", "application/zip", 200 * MB))
        self.assertTrue(meta["path"].startswith(os.path.join(self.state, "attachments") + os.sep))
        got = hashlib.sha256()
        with open(meta["path"], "rb") as f:
            for chunk in iter(lambda: f.read(MB), b""):
                got.update(chunk)
        self.assertEqual(got.hexdigest(), want.hexdigest())

    def test_the_answer_is_what_upload_gives_and_send_takes_it(self):
        code, meta = self.raw("?name=" + quote("εικόνα 1.png") + "&mime=image/png", b"\x89PNG data")
        self.assertEqual(code, 200)
        self.assertEqual(set(meta), {"path", "name", "mime", "size"})
        self.assertEqual((meta["name"], meta["mime"], meta["size"]), ("εικόνα 1.png", "image/png", 9))
        self.assertEqual(self.web.own_file(meta["path"]), os.path.realpath(meta["path"]))   # /api/send will accept it

    def test_mime_falls_back_to_content_type(self):
        code, meta = self.raw("?name=a.txt", b"hi", headers={"Content-Type": "text/plain"})
        self.assertEqual((code, meta["mime"]), (200, "text/plain"))

    def test_name_with_dots_and_special_characters_stays_inside_its_folder(self):
        for name, want in (("../../etc/passwd", "passwd"), ("..\\..\\win.ini", "win.ini"),
                           ('a<b>:"c"|?*.txt', "a_b___c____.txt"), ("..", "file"), ("", "file")):
            code, meta = self.raw("?name=" + quote(name), b"x")
            self.assertEqual(code, 200, name)
            self.assertEqual(meta["name"], want, name)
            self.assertEqual(os.path.dirname(os.path.dirname(meta["path"])), os.path.join(self.state, "attachments"))
            self.assertEqual(os.path.basename(meta["path"]), want)

    def test_one_name_rule_for_both_uploads(self):
        import base64
        meta, _ = self.web.save_upload({"name": '..\\x<y>.png', "data": base64.b64encode(b"x").decode()})
        code, raw = self.raw("?name=" + quote('..\\x<y>.png'), b"x")
        self.assertEqual(raw["name"], meta["name"])

    def test_a_long_name_fits_the_file_system_and_keeps_its_extension(self):
        from modules.web.attach import clean_name
        name = clean_name("文" * 110 + ".pdf")
        self.assertLessEqual(len(name.encode()), 200)
        self.assertTrue(name.endswith("文.pdf"))
        code, meta = self.raw("?name=" + quote("文" * 110 + ".pdf"), b"x")
        self.assertEqual((code, meta["name"]), (200, name))
        self.assertEqual(clean_name("a" * 130 + ".pdf"), "a" * 116 + ".pdf")
        for odd, want in (("nul.txt", "_nul.txt"), ("CON", "_CON"), ("report. ", "report"), ("lpt1.a.b", "_lpt1.a.b")):
            self.assertEqual(clean_name(odd), want)

    def test_telegram_takes_the_same_name_rule(self):
        import inspect
        from modules.telegram import bot
        self.assertIn("attach.new_path", inspect.getsource(bot))

    def test_an_odd_web_section_in_hub_json_keeps_the_default(self):
        for web in (8787, True, "x", [1], {"upload_max_mb": "big"}, {"upload_max_mb": -1}, {"upload_max_mb": float("inf")}):
            self.cfg = {"web": web}
            self.assertEqual(self.web.with_clock({})["upload"]["max_bytes"], 200 * MB, web)

    def test_no_content_length_is_411(self):
        code, out = self.raw("?name=a", length=False, headers={"Transfer-Encoding": "chunked"},
                             send=lambda s: s.sendall(b"1\r\nx\r\n0\r\n\r\n"))
        self.assertEqual(code, 411)
        code, out = self.raw("?name=a", length=False)
        self.assertEqual(code, 411)
        self.assertEqual(self.files(), [])

    def test_a_length_that_is_not_ascii_digits_is_400_not_a_dropped_connection(self):
        # "²" is a digit to str.isdigit() and not a number to int(); the header is read as latin-1
        for length in (chr(0xb2), chr(0xb3) + chr(0xb9), "1" + chr(0xb2), "9" * 5000, "-1", "1.5", ""):
            code, out = self.raw("?name=a", b"x", length=length)
            self.assertEqual(code, 400, repr(length))
        self.assertEqual(self.files(), [])

    def test_over_the_limit_is_413_and_nothing_is_written(self):
        self.cfg = {"web": {"upload_max_mb": 1}}
        code, out = self.raw("?name=a", b"x" * (MB + 1))
        self.assertEqual(code, 413)
        self.assertIn("1 MB", out["error"])
        self.assertEqual(self.dirs(), [])
        code, out = self.raw("?name=a", b"x" * MB)
        self.assertEqual(code, 200)
        # the limit is not in settings — 200 MB; a larger one declared — refused at once, we do not wait for the body
        self.cfg = {}
        code, out = self.raw("?name=a", length=200 * MB + 1)
        self.assertEqual(code, 413)

    def test_empty_body_is_refused(self):
        code, out = self.raw("?name=a", b"")
        self.assertEqual(code, 400)
        self.assertEqual(self.dirs(), [])

    def test_without_x_hub_it_is_403(self):
        s = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        s.sendall(f"POST /api/upload/raw?name=a HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nContent-Length: {MB}\r\n\r\n".encode())
        data = b""
        while chunk := s.recv(65536):   # body unread, connection closed — it will not become the "next request"
            data += chunk
        s.close()
        self.assertTrue(data.startswith(b"HTTP/1.1 403"), data[:80])
        self.assertIn(b"Connection: close", data)
        self.assertEqual(self.dirs(), [])

    def test_a_client_gone_midway_leaves_no_file(self):
        seen = []

        def send(s):
            s.sendall(b"x" * (300 << 10))
            end = time.time() + 5
            while not self.files() and time.time() < end:
                time.sleep(0.02)
            seen.extend(self.files())
            s.shutdown(socket.SHUT_WR)
        code, _ = self.raw("?name=half.bin", length=MB, send=send)
        self.assertEqual(code, 0)   # nobody to answer; while the body was coming — only .part, not a file under the real name
        end = time.time() + 5
        while self.dirs() and time.time() < end:
            time.sleep(0.05)
        self.assertEqual(self.dirs(), [])
        self.assertEqual([os.path.basename(f) for f in seen], ["half.bin.part"])

    def test_snapshot_announces_raw_upload_and_its_limit(self):
        snap = self.web.with_clock({})
        self.assertIn("upload.raw", snap["features"])
        self.assertEqual(snap["upload"], {"raw": "/api/upload/raw", "max_bytes": 200 * MB, "max_inline": 20 * MB})
        self.cfg = {"web": {"upload_max_mb": 50}}
        self.assertEqual(self.web.with_clock({})["upload"]["max_bytes"], 50 * MB)


if __name__ == "__main__":
    unittest.main()
