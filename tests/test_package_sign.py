"""The signing of the Windows app's installer (app spec §6.3, slice A8): tools/package/sign.py gives the unsigned file
to the signing pipeline (a GitLab project: its packages, its trigger, its job's artifact) exactly once, and releases
the result only when it is the given file plus one valid signature of the publisher (tools/package/authenticode.py,
osslsigncode). GitLab is a fake on this machine, the signer a fake that writes a structurally real Authenticode
signature with a dummy certificate part, osslsigncode a fake answer: no network, no key."""
import tests  # noqa: F401 - keeps the machine's settings out
import contextlib
import hashlib
import http.server
import io
import json
import os
import struct
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.parse
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(REPO, "tools", "package")
sys.path.insert(0, PKG)
import authenticode  # noqa: E402
import sign  # noqa: E402


def tlv(tag, body):
    n = len(body)
    head = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + head + body


def pe(body=b"\x90" * 1000, overlay=b""):
    """A small PE32+ program: headers, one section, an optional overlay (where an installer keeps its data)."""
    b = bytearray(0x200)
    b[:2] = b"MZ"
    struct.pack_into("<I", b, 0x3C, 0x80)
    b[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", b, 0x84, 0x8664, 1, 0, 0, 0, 240, 0x22)
    opt = 0x98
    struct.pack_into("<H", b, opt, 0x20B)
    struct.pack_into("<I", b, opt + 64, 0x1234)            # a checksum the signer rewrites
    struct.pack_into("<I", b, opt + 108, 16)                # data directories
    sec = opt + 240
    b[sec:sec + 8] = b".text\0\0\0"
    struct.pack_into("<IIII", b, sec + 8, len(body), 0x1000, len(body), 0x200)
    return bytes(b) + body + overlay


def pkcs7(digest_hex, algo_oid=bytes.fromhex("0609608648016503040201")):
    """A SignedData whose SpcIndirectDataContent claims the digest; the certificates and signer are a dummy."""
    spc = tlv(0x30, tlv(0x30, tlv(0x06, bytes.fromhex("2b06010401823702010f")))
              + tlv(0x30, tlv(0x30, algo_oid + b"\x05\x00") + tlv(0x04, bytes.fromhex(digest_hex))))
    content = tlv(0x30, authenticode.SPC_INDIRECT + tlv(0xA0, spc))
    signed = tlv(0x30, tlv(0x02, b"\x01") + tlv(0x31, b"") + content + tlv(0xA0, b"\x30\x00") + tlv(0x31, b""))
    return tlv(0x30, authenticode.SIGNED_DATA + tlv(0xA0, signed))


def signed_copy(data, digest_hex=None, tamper=None, after=b""):
    """What a signer makes of data: padded to 8, the checksum rewritten, the table appended and named."""
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(data)
    try:
        digest_hex = digest_hex or authenticode.digest(f.name)
    finally:
        os.unlink(f.name)
    b = bytearray(data + b"\0" * (-len(data) % 8))
    blob = pkcs7(digest_hex)
    entry = struct.pack("<IHH", 8 + len(blob), 0x0200, 0x0002) + blob
    entry += b"\0" * (-len(entry) % 8)
    at = len(b)
    b += entry
    opt = 0x98
    struct.pack_into("<I", b, opt + 64, 0xBEEF)
    struct.pack_into("<II", b, opt + 112 + 32, at, len(entry))
    if tamper is not None:
        b[tamper] ^= 0xFF
    return bytes(b) + after


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return path


class Authenticode(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hub-sign-")
        self.addCleanup(__import__("shutil").rmtree, self.dir)
        self.data = pe(overlay=b"installer data" * 7)         # 1239 bytes: not a multiple of 8
        self.unsigned = write(os.path.join(self.dir, "a.exe"), self.data)

    def signed(self, *a, **kw):
        return write(os.path.join(self.dir, "a.signed.exe"), signed_copy(self.data, *a, **kw))

    def test_the_signed_file_is_the_given_one_plus_a_signature_over_it(self):
        s = self.signed()
        mine = authenticode.digest(self.unsigned)
        self.assertEqual(authenticode.digest(s), mine)          # the checksum and the table are not hashed
        self.assertEqual(authenticode.signature(s)[:2], ("sha256", mine))
        self.assertIsNone(authenticode.same_program(self.unsigned, s))

    def test_a_signed_file_that_differs_anywhere_hashed_is_refused(self):
        for where in (0x200 + 10, len(self.data) - 1, 0x80 + 30):   # a section, the overlay, a header
            with self.subTest(where), self.assertRaisesRegex(authenticode.Bad, "differs|claims"):
                authenticode.same_program(self.unsigned, self.signed(tamper=where))

    def test_a_signature_claiming_another_digest_is_refused(self):
        with self.assertRaisesRegex(authenticode.Bad, "claims"):
            authenticode.same_program(self.unsigned, self.signed("ab" * 32))

    def test_bytes_after_the_signature_are_refused(self):
        with self.assertRaisesRegex(authenticode.Bad, "after the certificate table"):
            authenticode.signature(self.signed(after=b"more code"))

    def test_an_unsigned_or_already_signed_input_is_named(self):
        with self.assertRaisesRegex(authenticode.Bad, "not signed"):
            authenticode.signature(self.unsigned)
        s = self.signed()
        with self.assertRaisesRegex(authenticode.Bad, "already signed"):
            authenticode.same_program(s, s)
        with self.assertRaisesRegex(authenticode.Bad, "not a program"):
            authenticode.digest(write(os.path.join(self.dir, "x.exe"), b"<html>"))


class FakeGitLab(http.server.ThreadingHTTPServer):
    """The signing project's API: generic packages, the pipeline trigger, its jobs, the signing job's artifact."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.packages, self.deleted, self.triggers = {}, [], []
        self.outcome = "signed"          # signed | failed | tampered | sums | later
        self.polls = 0
        self.trigger_status = 201

    def jobs(self, pid):
        self.polls += 1
        n = self.polls
        if self.outcome == "later" and n < 1000:
            return [{"id": 1, "name": "fetch", "status": "success"}, {"id": 2, "name": "sign-win",
                                                                     "status": "waiting_for_resource"}]
        if n == 1:
            return [{"id": 1, "name": "fetch", "status": "running"}, {"id": 2, "name": "sign-win", "status": "created"}]
        if self.outcome == "failed":
            return [{"id": 1, "name": "fetch", "status": "success"}, {"id": 2, "name": "sign-win", "status": "failed"}]
        return [{"id": 1, "name": "fetch", "status": "success"}, {"id": 2, "name": "sign-win", "status": "success"}]

    def artifact(self):
        var = self.triggers[-1]
        path = urllib.parse.urlsplit(var["variables[ARTIFACT_URL]"]).path
        data = self.packages[path.split("/packages/generic/", 1)[1]]
        assert hashlib.sha256(data).hexdigest() == var["variables[ARTIFACT_SHA256]"]   # the pipeline's fetch checks it
        name = path.rsplit("/", 1)[1]
        body = signed_copy(data, tamper=0x200 + 5 if self.outcome == "tampered" else None)
        digest = hashlib.sha256(body).hexdigest() if self.outcome != "sums" else "0" * 64
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            z.writestr(f"signed/{name}", body)
            z.writestr("signed/SHA256SUMS.txt", f"{digest}  {name}\n")
        return out.getvalue()


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=b"", kind="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def path_of(self):
        prefix = "/api/v4/projects/7"
        path = urllib.parse.urlsplit(self.path).path
        assert path.startswith(prefix), path
        return path[len(prefix):]

    def authorized(self):
        if self.headers.get("PRIVATE-TOKEN") != "api-secret":
            self.reply(401, {"message": "401 Unauthorized"})
            return False
        return True

    def do_PUT(self):
        s, path = self.server, self.path_of()
        data = self.rfile.read(int(self.headers["Content-Length"]))
        if not self.authorized():
            return
        s.packages[path.split("/packages/generic/", 1)[1]] = data
        self.reply(201, {"id": 90, "package_id": 41, "file_name": path.rsplit("/", 1)[1]})

    def do_POST(self):
        s, path = self.server, self.path_of()
        form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
        assert path == "/trigger/pipeline" and "PRIVATE-TOKEN" not in self.headers
        if form.get("token") != "trigger-secret":
            return self.reply(404, {"message": "404 Not Found"})
        s.triggers.append(form)
        if s.trigger_status != 201:
            return self.reply(s.trigger_status, {"message": "a proxy said no"})
        self.reply(201, {"id": 500 + len(s.triggers), "web_url": "https://gitlab.example.test/p/-/pipelines/1"})

    def do_GET(self):
        s, path = self.server, self.path_of()
        if not self.authorized():
            return
        if path.startswith("/pipelines/") and path.endswith("/jobs"):
            return self.reply(200, s.jobs(int(path.split("/")[2])))
        if path == "/jobs/2/trace":
            return self.reply(200, b"fetch ok\nERROR: signtool sign exited with 1 on a.exe. Stopping, no retry.\n",
                              "text/plain")
        if path == "/jobs/2/artifacts" and s.outcome == "cut":
            return self.reply(200, b"<html>a proxy's page</html>", "application/zip")
        if path == "/jobs/2/artifacts" and s.outcome != "gone":
            return self.reply(200, s.artifact(), "application/zip")
        self.reply(404, {"message": "404 Not Found"})

    def do_DELETE(self):
        if self.authorized():
            self.server.deleted.append(self.path_of())
            self.reply(204)


OSSL_OK = """Signature Index: 0  (Primary Signature)
Signer's certificate:
\tSigner #0:
\t\tSubject: CN=Example Org,O=Example Org,ST=Somewhere,C=US
\t\tIssuer : CN=Example Code Signing CA,O=Example CA,C=GB
Countersignatures:
\tTimestamp time: Nov  8 05:31:39 2026 GMT
Signature verification: ok

Number of verified signatures: 1
Succeeded
"""


class Signing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hub-sign-")
        self.addCleanup(__import__("shutil").rmtree, self.dir)
        self.gl = FakeGitLab()
        threading.Thread(target=self.gl.serve_forever, daemon=True).start()
        self.addCleanup(self.gl.server_close)
        self.addCleanup(self.gl.shutdown)
        self.root_pem = write(os.path.join(self.dir, "root.pem"), b"a code signing root")
        self.tsa_pem = write(os.path.join(self.dir, "tsa.pem"), b"timestamp roots")
        self.cfg = {"gitlab": f"http://127.0.0.1:{self.gl.server_port}", "project": 7, "ref": "main", "job": "sign-win",
                    "package": "hub-unsigned", "publisher": "Example Org", "cafile": self.root_pem,
                    "tsa_cafile": self.tsa_pem, "osslsigncode": "osslsigncode",
                    "ledger": os.path.join(self.dir, "signed.json"), "wait_minutes": 5,
                    "trigger_token": "trigger-secret", "api_token": "api-secret"}
        self.exe = write(os.path.join(self.dir, "agents-hub-v1.9.0-windows-x64-setup.exe"), pe(overlay=b"setup" * 50))
        self.out = os.path.join(self.dir, "out", os.path.basename(self.exe))
        self.ossl_says, self.ossl = OSSL_OK, []
        self.t = [0.0]

    def run_ossl(self, args, **kw):
        if args[1:] == ["--version"]:
            return __import__("subprocess").CompletedProcess(args, 0, "osslsigncode 2.13, using:\n", "")
        self.ossl.append(args)
        return __import__("subprocess").CompletedProcess(args, 0 if "verification: ok" in self.ossl_says else 1,
                                                         self.ossl_says, "")

    def sign(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()):
            return sign.sign(self.exe, self.cfg, out=self.out, run=self.run_ossl, sleep=self.tick,
                             clock=lambda: self.t[0], **kw)

    def tick(self, s):
        self.t[0] += s

    def refused(self, code=3, **kw):
        with self.assertRaises(sign.Stop) as e:
            self.sign(**kw)
        self.assertEqual(e.exception.code, code, str(e.exception))
        return str(e.exception)

    def test_one_pipeline_signs_and_the_checked_result_is_written(self):
        self.assertEqual(self.sign(), self.out)
        (form,) = self.gl.triggers
        digest = hashlib.sha256(open(self.exe, "rb").read()).hexdigest()
        self.assertEqual(form["variables[ARTIFACT_SHA256]"], digest)
        self.assertEqual(form["ref"], "main")
        url = form["variables[ARTIFACT_URL]"]
        self.assertTrue(url.startswith(self.cfg["gitlab"] + "/api/v4/projects/7/packages/generic/hub-unsigned/"), url)
        self.assertTrue(url.endswith("/" + os.path.basename(self.exe)), url)   # the pipeline names the file from it
        self.assertEqual(self.gl.deleted, ["/packages/41"])                   # the package goes once it is taken
        self.assertIsNone(authenticode.same_program(self.exe, self.out))
        self.assertEqual(os.stat(self.out).st_mode & 0o777, os.stat(self.exe).st_mode & 0o777)
        (args,) = self.ossl
        self.assertEqual(args[:6], ["osslsigncode", "verify", "-CAfile", self.root_pem, "-TSA-CAfile", self.tsa_pem])
        self.assertFalse(os.path.exists(self.cfg["ledger"] + ".lock"))
        with open(self.cfg["ledger"]) as f:
            self.assertEqual(json.load(f)[digest]["status"], "signed")
        self.sign()                                    # the same file again: its result, no second signing
        self.assertEqual(len(self.gl.triggers), 1)

    def test_a_failed_signing_stops_with_its_reason_and_is_never_started_again_by_itself(self):
        self.gl.outcome = "failed"
        why = self.refused()
        self.assertIn("signtool sign exited with 1", why)        # the job's own words
        self.assertIn("--again", why)
        self.assertEqual(len(self.gl.triggers), 1)
        self.assertEqual(self.gl.deleted, ["/packages/41"])
        self.assertFalse(os.path.exists(self.out))
        self.assertIn("already given to the signing", self.refused())
        self.assertEqual(len(self.gl.triggers), 1)                # the second call started nothing
        self.gl.outcome, self.gl.polls = "signed", 0
        self.sign(again=True)                                     # a person's deliberate new attempt
        self.assertEqual(len(self.gl.triggers), 2)

    def test_a_result_that_is_not_the_given_file_plus_a_signature_is_not_released(self):
        for outcome, said in (("tampered", "not the given one"), ("sums", "SHA256SUMS")):
            self.gl.outcome, self.gl.polls = outcome, 0
            with self.subTest(outcome):
                self.assertIn(said, self.refused(again=True))
                self.assertFalse(os.path.exists(self.out))
        self.assertEqual(self.ossl, [])                           # not even shown to osslsigncode
        with open(self.cfg["ledger"]) as f:
            self.assertEqual({e["status"] for e in json.load(f).values()}, {"rejected"})

    def test_a_signature_osslsigncode_does_not_take_is_not_released(self):
        for says, said in ((OSSL_OK.replace("O=Example Org", "O=Another Org"), "not 'Example Org'"),
                           (OSSL_OK.replace("Signature verification: ok", "Signature verification: failed"),
                            "not valid"),
                           (OSSL_OK.replace("\tTimestamp time: Nov  8 05:31:39 2026 GMT\n", ""), "no timestamp")):
            self.ossl_says, self.gl.polls = says, 0
            with self.subTest(said):
                self.assertIn(said, self.refused(again=True))
                self.assertFalse(os.path.exists(self.out))

    def test_a_wait_that_runs_out_is_taken_up_again_without_a_new_signing(self):
        self.gl.outcome = "later"
        self.assertIn("still waiting_for_resource", self.refused(code=4))
        self.assertEqual(self.gl.deleted, [])                     # the signing job has not ended: the file stays
        self.gl.outcome, self.gl.polls = "signed", 1
        self.assertEqual(self.sign(), self.out)
        self.assertEqual(len(self.gl.triggers), 1)
        self.assertEqual(self.gl.deleted, ["/packages/41"])

    def test_a_trigger_that_did_not_answer_is_not_repeated(self):
        self.gl.trigger_status = 502
        self.assertIn("whether a pipeline started is not known", self.refused(code=4))
        self.assertEqual(self.gl.deleted, ["/packages/41"])
        self.gl.trigger_status = 201
        self.assertIn("already given to the signing", self.refused())
        self.assertEqual(len(self.gl.triggers), 1)

    def test_an_artifact_cut_on_the_way_is_taken_again_without_a_new_signing(self):
        self.gl.outcome = "cut"
        self.assertIn("not a whole zip", self.refused(code=4))
        self.assertFalse(os.path.exists(self.out))
        self.gl.outcome = "signed"
        self.assertEqual(self.sign(), self.out)
        self.assertEqual(len(self.gl.triggers), 1)

    def test_a_run_that_died_after_the_trigger_is_not_repeated(self):
        real = sign.GitLab.trigger

        def dies(api, *a):
            real(api, *a)                                   # the pipeline starts, the answer never comes back
            raise KeyboardInterrupt
        with unittest.mock.patch.object(sign.GitLab, "trigger", dies), self.assertRaises(KeyboardInterrupt):
            self.sign()
        self.assertIn("unknown", self.refused())
        self.assertEqual(len(self.gl.triggers), 1)

    def test_a_signed_file_whose_artifact_is_gone_is_signed_again_only_with_again(self):
        self.sign()
        os.unlink(self.out)
        self.gl.outcome = "gone"
        self.assertIn("--again", self.refused(code=4))
        self.assertEqual(len(self.gl.triggers), 1)
        self.gl.outcome, self.gl.polls = "signed", 0
        self.sign(again=True)
        self.assertEqual(len(self.gl.triggers), 2)
        self.assertIsNone(authenticode.same_program(self.exe, self.out))

    def test_what_would_keep_the_check_from_running_stops_before_any_signing(self):
        write(self.cfg["ledger"] + ".lock", b"4242")
        self.assertIn("another signing runs", self.refused(code=4))
        os.unlink(self.cfg["ledger"] + ".lock")
        write(self.cfg["ledger"], b"{broken")
        self.assertIn("not readable", self.refused(code=2))
        os.unlink(self.cfg["ledger"])
        for key in ("cafile", "tsa_cafile"):
            was, self.cfg[key] = self.cfg[key], os.path.join(self.dir, "none.pem")
            with self.subTest(key):
                self.assertIn(f"no {key}", self.refused(code=2))
            self.cfg[key] = was
        self.cfg["osslsigncode"] = os.path.join(self.dir, "no-such-program")
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(sign.Stop) as e:
            sign.sign(self.exe, self.cfg, out=self.out)
        self.assertEqual(e.exception.code, 2)
        self.assertEqual((self.gl.packages, self.gl.triggers), ({}, []))
        self.assertFalse(os.path.exists(self.cfg["ledger"] + ".lock"))

    def test_the_publisher_is_read_from_either_form_of_the_subject(self):
        for subject, org in (("CN=Example Org,O=Example Org,C=US", "Example Org"),
                             ("/C=US/O=Example Org/CN=Example Org", "Example Org"),
                             ("CN=Acme\\, Inc.,O=Acme\\, Inc.,C=US", "Acme, Inc."), ("CN=Example Org,C=US", None)):
            with self.subTest(subject):
                self.assertEqual(sign.subject_field(subject, "O"), org)

    def test_a_wrong_input_never_reaches_the_pipeline(self):
        signed = write(os.path.join(self.dir, "s.exe"), signed_copy(pe()))
        for path, said in ((write(os.path.join(self.dir, "a b.exe"), pe()), "file name"),
                           (signed, "already signed"), (write(os.path.join(self.dir, "t.exe"), b"text"), "not a program")):
            self.exe = path
            with self.subTest(said):
                self.assertIn(said, self.refused(code=2))
        self.assertEqual((self.gl.packages, self.gl.triggers), ({}, []))


class Settings(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="hub-sign-")
        self.addCleanup(__import__("shutil").rmtree, self.dir)
        self.path = os.path.join(self.dir, "sign-windows.json")
        self.env = write(os.path.join(self.dir, "trigger.env"), b"HUB_SIGNING_TRIGGER_TOKEN=trigger-secret\n")
        self.exe = write(os.path.join(self.dir, "a.exe"), pe())

    def save(self, **over):
        cfg = {"gitlab": "https://gitlab.example.test", "project": 7, "job": "sign-win", "package": "hub-unsigned",
               "publisher": "Example Org", "cafile": "~/root.pem", "trigger_env": self.env,
               "api_token_command": [sys.executable, "-c", "print('api-secret')"], **over}
        with open(self.path, "w") as f:
            json.dump({k: v for k, v in cfg.items() if v is not None}, f)

    def test_the_tokens_come_from_their_file_and_command(self):
        self.save()
        cfg = sign.settings(self.path, environ={})
        self.assertEqual((cfg["trigger_token"], cfg["api_token"]), ("trigger-secret", "api-secret"))
        self.assertEqual(cfg["cafile"], os.path.expanduser("~/root.pem"))
        self.assertEqual(sign.settings(self.path, environ={"HUB_SIGNING_TRIGGER_TOKEN": "t2"})["trigger_token"], "t2")

    def test_no_settings_or_no_token_leaves_the_file_unsigned_in_one_line(self):
        before = open(self.exe, "rb").read()
        for what in ("no settings", "no token"):
            if what == "no token":
                self.save(trigger_env=os.path.join(self.dir, "none.env"))
            out = io.StringIO()
            with self.subTest(what), contextlib.redirect_stdout(out), mock_env():
                self.assertFalse(sign.step(self.exe, settings_path=self.path))
                self.assertIn("Windows signing is off here", out.getvalue())
                self.assertIn("stays unsigned", out.getvalue())
                with self.assertRaises(sign.Stop) as e:
                    sign.step(self.exe, settings_path=self.path, require=True)
                self.assertEqual(e.exception.code, 2)
        self.assertEqual(open(self.exe, "rb").read(), before)

    def test_wrong_settings_are_named(self):
        for over in ({"gitlab": "http://gitlab.example.test"}, {"publisher": ""}, {"package": "a/b"}):
            self.save(**over)
            with self.subTest(over), self.assertRaises(sign.Stop):
                sign.settings(self.path, environ={})


@contextlib.contextmanager
def mock_env():
    keep = {k: os.environ.pop(k) for k in (sign.TOKEN_VAR, sign.API_VAR) if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(keep)


class Upload(unittest.TestCase):
    def test_an_unsigned_windows_app_is_not_sent_to_the_team_server(self):
        import deliver_send
        d = tempfile.mkdtemp(prefix="hub-sign-")
        self.addCleanup(__import__("shutil").rmtree, d)
        key = write(os.path.join(d, "key"), b"k" * 32)
        os.chmod(key, 0o600)
        exe = write(os.path.join(d, "agents-hub-v1.9.0-windows-x64-setup.exe"), pe())
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = deliver_send.main(["http://127.0.0.1:9", "--key-file", key, "app", exe, "--platform", "windows-x64",
                                      "--runtime", "2"])
        self.assertEqual(code, 2)
        self.assertIn("not signed", err.getvalue())
        write(exe, signed_copy(pe(), tamper=0x200 + 3))       # changed after it was signed
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = deliver_send.main(["http://127.0.0.1:9", "--key-file", key, "app", exe, "--platform", "windows-x64",
                                      "--runtime", "2"])
        self.assertEqual(code, 2)
        self.assertIn("changed after it was signed", err.getvalue())


if __name__ == "__main__":
    unittest.main()
