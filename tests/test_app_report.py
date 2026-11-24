"""The app's failed update goes to the company's Sentry, and the open minimum version (app spec §7, slice A5).

A failed step of the update on a code layer (the releases unreadable, a broken or damaged download, a layer that did
not come up, the release put back on an edited layer) is one Sentry event per version and step: the whole report,
unmasked (the owner's decision of 2026-11-07: it is an internal tool) — only the build key is cut from it. Passing
failures (no network, a 5xx) are not reported; a report that could not be sent waits for the next pass. update.report:
false and a shell without the address (the open build) send nothing.

min.json of the distribution server (open, without the key): a hub below the minimum says so on every check — the page
draws a band, Telegram a message once a day with "Update"; the version the minimum asks for is offered at once, past
the stable delay. The A3 stand (tests/test_app_update.py): real layers, the real delivery server, a fake Sentry
receiver on 127.0.0.1."""
import tests  # noqa: F401 - keeps the machine's settings out
import gzip
import http.server
import json
import os
import tempfile
import threading
import unittest

import platforms
from tests import test_app_update as base

HOUR = 3600
SENTRY_KEY = "0123456789abcdef0123456789abcdef"


class Sentry:
    """A Sentry receiver: POST /api/<project>/envelope/ with X-Sentry-Auth; answers `status` (200 by default)."""

    def __init__(self):
        self.got, self.status = [], 200
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                if self.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                outer.got.append({"path": self.path, "auth": self.headers.get("X-Sentry-Auth"),
                                  "type": self.headers.get("Content-Type"), "body": body})
                self.send_response(outer.status)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()   # a quick shutdown: the default poll is 0.5 s
        self.dsn = f"http://{SENTRY_KEY}@127.0.0.1:{self.srv.server_address[1]}/42"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()

    def events(self):
        """[(event, {filename: bytes})] of every envelope received."""
        out = []
        for g in self.got:
            lines = g["body"].split(b"\n")
            head, i, event, files = json.loads(lines[0]), 1, None, {}
            rest = g["body"][len(lines[0]) + 1:]
            while rest:
                item_line, _, rest = rest.partition(b"\n")
                item = json.loads(item_line)
                # as Sentry reads it: the payload is exactly its length, then a newline
                assert rest[item["length"]:item["length"] + 1] == b"\n", "missing newline after the payload"
                payload, rest = rest[:item["length"]], rest[item["length"] + 1:]
                if item["type"] == "event":
                    event = json.loads(payload)
                    assert event["event_id"] == head["event_id"]
                elif item["type"] == "attachment":
                    files[item["filename"]] = payload
                i += 1
            out.append((event, files))
        return out


class Stand(base.Stand):
    def setUp(self):
        super().setUp()
        self.sentry = Sentry()
        self.addCleanup(self.sentry.close)
        self.deliver(report=self.sentry.dsn)
        os.makedirs(os.path.join(self.state, "logs"), exist_ok=True)
        base.write(os.path.join(self.state, "logs", "hub-up.log"),
                   "hub-up: web started\nupdate: available\nsomething else\nupdate: bad-archive\n")
        base.write(os.path.join(self.state, "logs", "update-trial.log"), "trial: rolled-back\n")

    def deliver(self, **extra):
        base.write(os.path.join(self.codes(), "deliver.json"),
                   json.dumps({"url": self.url, "key": base.BUILD_KEY, **extra}))

    def damage(self, tag="1.3.0"):
        stored = os.path.join(self.root, "deliver", "files", "layer", f"code-{tag}-team.tar.gz")
        os.chmod(stored, 0o644)
        with open(stored, "r+b") as f:
            f.seek(100)
            f.write(b"\0" * 64)


class Report(Stand):
    def setUp(self):
        super().setUp()
        self.release("v1.3.0")
        self.publish("v1.3.0")

    def test_a_damaged_download_is_one_full_report_without_the_build_key(self):
        self.damage()
        self.assertEqual(self.updater().check(apply=True, manual=True), "bad-archive")
        self.assertEqual(len(self.sentry.got), 1)
        got = self.sentry.got[0]
        self.assertEqual(got["path"], "/api/42/envelope/")
        self.assertIn(f"sentry_key={SENTRY_KEY}", got["auth"])
        self.assertEqual(got["type"], "application/x-sentry-envelope")
        self.assertNotIn(base.BUILD_KEY.encode(), got["body"])    # the build key is access, not a path or an error
        (event, files), = self.sentry.events()
        self.assertEqual(event["level"], "error")
        tags = event["tags"]
        self.assertEqual((tags["step"], tags["from"], tags["to"], tags["flavor"], tags["channel"]),
                         ("verify", "1.2.3", "1.3.0", "team", "stable"))
        self.assertEqual(event["fingerprint"], ["update", "verify", "1.3.0"])
        extra = event["extra"]
        self.assertIn("sha256", extra["error"])                      # the text of the error as it is
        self.assertEqual(extra["result"], "bad-archive")
        self.assertIn(json.dumps(self.state)[1:-1], json.dumps(extra))   # real paths: no mask (as JSON writes them)
        for key in ("free_bytes", "claude", "machine", "user", "plugins", "modules", "layer", "runtime"):
            self.assertIn(key, extra, key)
        self.assertEqual(extra["layer"]["running"], "1.2.3-team")
        # the update's lines of hub-up.log and the watchdog's log go along whole
        self.assertEqual(files["update.log"].decode().splitlines(),
                         ["hub-up: web started", "update: available", "update: bad-archive"])
        self.assertIn(b"trial: rolled-back", files["update-trial.log"])

    def test_the_same_version_and_step_again_is_counted_not_sent_and_the_count_goes_with_the_next(self):
        self.damage()
        self.updater().check(apply=True, manual=True)
        self.updater().check(apply=True, manual=True)
        self.updater().check(apply=True, manual=True)
        self.assertEqual(len(self.sentry.got), 1)
        self.sentry.srv.shutdown()   # the next: the server breaks off — another step of the same version
        self.sentry.srv.server_close()
        self.sentry = Sentry()
        self.addCleanup(self.sentry.close)
        self.deliver(report=self.sentry.dsn)
        u = self.updater()
        u.small = lambda url, private=None: (200, b"[not json")
        self.assertEqual(u.check(apply=True, manual=True), "error")
        (event, _), = self.sentry.events()
        self.assertEqual(event["tags"]["step"], "check")
        self.assertEqual(event["extra"]["repeats"], {"1.3.0|verify": 2})

    def test_passing_failures_are_not_reported(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.assertEqual(self.updater().check(apply=True, manual=True), "offline")
        u = self.updater()
        u.small = lambda url, private=None: (503, b"busy")
        self.assertEqual(u.check(apply=True, manual=True), "error")
        self.assertEqual(self.sentry.got, [])

    def test_the_key_cut_from_an_error_keeps_the_envelope_whole(self):
        # a failed download names its address with the key (a link of the installer): cut, and the lengths still hold
        u = self.updater()

        def broken(url, dest, sha, size, what, again, private=None, progress=None, log=None):
            log("attempt 1: HTTP 206 after 0.1 s")
            log("attempt 1: The read operation timed out after 60.1 s (have 0 of 16005168, window 4000000)")
            raise u.fetch_failed("network", f"GET {url}?key={base.BUILD_KEY}: cut at 4 MB")
        u.download = broken
        self.assertEqual(u.check(apply=True, manual=True), "broken")
        self.assertNotIn(base.BUILD_KEY.encode(), self.sentry.got[0]["body"])
        (event, files), = self.sentry.events()
        self.assertEqual(event["tags"]["step"], "download")
        self.assertIn("key=[build key]", event["extra"]["error"])
        self.assertEqual(event["exception"]["values"][0]["type"], "Failed")
        # what each attempt met: whether the answer came and how late, where the silence was
        self.assertEqual(len(event["extra"]["attempts"]), 2)
        self.assertIn("HTTP 206 after 0.1 s", event["extra"]["attempts"][0])

    def test_a_release_the_server_refuses_to_read_is_a_check_report(self):
        self.deliver(report=self.sentry.dsn)
        base.write(os.path.join(self.codes(), "deliver.json"),
                   json.dumps({"url": self.url, "key": "another-key-0000000", "report": self.sentry.dsn}))
        self.assertEqual(self.updater().check(apply=False, manual=True), "error")
        (event, _), = self.sentry.events()
        self.assertEqual(event["tags"]["step"], "check")
        self.assertIn("401", event["extra"]["error"])
        self.assertNotIn("another-key", json.dumps(event))

    def test_off_in_the_settings_or_without_an_address_nothing_goes(self):
        self.damage()
        self.updater(report=False).check(apply=True, manual=True)
        self.deliver()                              # the open build: its shell names no address
        self.updater().check(apply=True, manual=True)
        self.assertEqual(self.sentry.got, [])
        data = self.events("update.checked")[-1]["data"]
        self.assertNotIn("report", data)            # the page has no line about reports

    def test_the_page_learns_that_reports_go_and_whether_they_are_off(self):
        self.updater().check(apply=False, manual=True)
        self.assertEqual(self.events("update.checked")[-1]["data"]["report"], {"on": True})
        self.updater(report=False).check(apply=False, manual=True)
        self.assertEqual(self.events("update.checked")[-1]["data"]["report"], {"on": False})

    def test_an_own_address_in_the_settings_takes_the_open_build_too(self):
        self.deliver()
        self.damage()
        self.updater(report_url=self.sentry.dsn).check(apply=True, manual=True)
        self.assertEqual(len(self.sentry.got), 1)

    def test_a_report_that_could_not_go_goes_at_the_next_pass(self):
        self.sentry.status = 503
        self.damage()
        self.updater().check(apply=True, manual=True)
        self.assertEqual(len(self.sentry.got), 1)
        self.sentry.status = 200
        self.updater().check(apply=False, manual=True)   # a pass with nothing new to report: the waiting one goes
        self.assertEqual(len(self.sentry.got), 2)
        self.updater().check(apply=False, manual=True)
        self.assertEqual(len(self.sentry.got), 2)
        self.assertEqual(self.sentry.events()[1][0]["tags"]["step"], "verify")

    def test_a_layer_that_did_not_come_up_is_reported_by_the_previous_code(self):
        self.updater().check(apply=True, manual=True)
        from core import appcode
        from modules.update import trial
        appcode.take_next(self.state)
        trial.roll_layer(self.state, self.read("update", "trial.json"), "timeout")
        self.updater().recover()
        (event, files), = self.sentry.events()
        self.assertEqual((event["tags"]["step"], event["tags"]["from"], event["tags"]["to"]), ("trial", "1.2.3", "1.3.0"))
        self.assertEqual(event["extra"]["layer"]["running"], "1.2.3-team")
        self.assertIn("1.3.0-team", json.dumps(event["extra"]["failed"]))

    def test_a_layer_that_did_not_come_up_carries_what_hub_up_saw_of_the_parts(self):
        # the report's own question is which part did not come up: hub-up's lines (exit codes) and a part's traceback
        # go along; the other lines of that log (a session's words in a watch line) do not
        base.write(os.path.join(self.state, "logs", "hub-up.log"), "\n".join([
            "13.11 08:31:30 hub-up: hubd started (pid 11)",
            "13.11 08:31:32 watch: a-session - the result waits for the owner: secret words of a session",
            "Traceback (most recent call last):",
            '  File "/x/core/daemon.py", line 5, in start',
            "    self.secret_source_line()",
            "ValueError: boom",
            "13.11 08:31:35 hub-up: hubd exited with code 1, again in 2 s", ""]))
        self.updater().check(apply=True, manual=True)
        from core import appcode
        from modules.update import trial
        appcode.take_next(self.state)
        trial.roll_layer(self.state, self.read("update", "trial.json"), "timeout")
        self.updater().recover()
        (_, files), = self.sentry.events()
        text = files["update.log"].decode()
        self.assertIn("hub-up: hubd exited with code 1, again in 2 s", text)
        self.assertIn("hub-up: hubd started (pid 11)", text)
        self.assertIn('File "/x/core/daemon.py", line 5, in start', text)
        self.assertIn("ValueError: boom", text)
        self.assertNotIn("secret words", text)

    def test_the_release_put_back_because_the_core_did_not_start_is_reported_once(self):
        base.write(os.path.join(self.codes(), "restored.json"), json.dumps(
            {"name": "1.2.3-team", "at": self.clock[0], "why": "start", "count": 2, "paths": ["core/x.py", "bin/hub-up"]}))
        self.updater().check(apply=False, manual=True)
        self.updater().check(apply=False, manual=True)
        (event, _), = self.sentry.events()
        self.assertEqual((event["tags"]["step"], event["tags"]["to"]), ("restore", "1.2.3"))
        self.assertEqual(event["extra"]["restored"]["paths"], ["core/x.py", "bin/hub-up"])

    def test_one_restore_record_is_one_report_not_a_repeat_each_pass(self):
        base.write(os.path.join(self.codes(), "restored.json"), json.dumps(
            {"name": "1.2.3-team", "at": self.clock[0], "why": "start", "count": 1, "paths": ["core/x.py"]}))
        for _ in range(3):
            self.updater().check(apply=False, manual=True)   # the record stays on disk for hubd
        self.damage()
        self.updater().check(apply=True, manual=True)
        self.assertEqual(len(self.sentry.got), 2)
        self.assertNotIn("repeats", self.sentry.events()[1][0]["extra"])

    def test_the_report_goes_after_the_check_is_written(self):
        self.damage()
        u = self.updater()
        written = []
        send = u.reporter.send
        u.reporter.send = lambda where, body: (written.append(len(self.events("update.checked"))), send(where, body))[1]
        u.check(apply=True, manual=True)
        self.assertEqual(written, [1])

    def test_a_receiver_that_does_not_answer_is_asked_once_a_pass(self):
        self.sentry.status = 503
        for tag in ("v1.3.1", "v1.3.2", "v1.3.3"):
            self.release(tag)
            self.publish(tag)
            self.damage(tag[1:])
            self.updater().check(apply=True, manual=True)
        self.assertEqual(len(self.sentry.got), 3)          # one try per pass
        before = len(self.sentry.got)
        self.updater().check(apply=False, manual=True)    # three wait; the first does not answer — the rest wait too
        self.assertEqual(len(self.sentry.got), before + 1)
        ledger = self.read("layers", "reports.json")
        self.assertEqual(len(ledger["pending"]), 3)
        self.assertLess(os.path.getsize(os.path.join(self.codes(), "reports.json")), 4096)   # bodies live apart
        self.sentry.status = 200
        self.updater().check(apply=False, manual=True)
        self.assertEqual(self.read("layers", "reports.json")["pending"], [])
        self.assertEqual(os.listdir(os.path.join(self.codes(), "reports")), [])

    def test_known_forms_of_keys_are_cut_and_the_paths_stay(self):
        with open(os.path.join(self.state, "logs", "hub-up.log"), "a") as f:
            f.write("update: fetch https://someone:hunter2hunter2@git.example.test/repo failed\n")
        self.damage()
        self.updater().check(apply=True, manual=True)
        body = self.sentry.got[0]["body"]
        self.assertNotIn(b"hunter2hunter2", body)
        self.assertIn(b"git.example.test/repo", body)
        self.assertIn(json.dumps(self.state, ensure_ascii=False)[1:-1].encode(), body)   # as the envelope writes it (a backslash doubled, letters whole)

    def test_an_error_while_switching_is_the_switch_of_that_version(self):
        u = self.updater()
        write = u._layers_write

        def full(name, data):   # the disk is full once the layer is whole: the history of the switch cannot be written
            if u.stage == "switch":
                raise OSError(28, "No space left on device")
            return write(name, data)
        u._layers_write = full
        self.assertEqual(u.check(apply=True, manual=True), "error")
        (event, _), = self.sentry.events()
        self.assertEqual((event["tags"]["step"], event["tags"]["to"]), ("switch", "1.3.0"))

    def test_a_restore_seen_while_reports_were_off_goes_once_they_are_on(self):
        base.write(os.path.join(self.codes(), "restored.json"), json.dumps(
            {"name": "1.2.3-team", "at": self.clock[0], "why": "start", "count": 1, "paths": ["core/x.py"]}))
        self.updater(report=False).check(apply=False, manual=True)
        self.updater().check(apply=False, manual=True)
        self.assertEqual(len(self.sentry.got), 1)

    def test_a_value_named_as_a_key_is_cut_and_a_stray_body_is_swept(self):
        from modules.update import report
        stray = os.path.join(self.codes(), "reports", "0" * 32 + ".envelope")
        base.write(stray, "left by a pass cut off")
        r = report.Reporter(self.state, {"update": {"enabled": True}}, now=lambda: self.clock[0])
        self.assertEqual(r.failed("verify", "1.3.0", None, "bad-archive", "x", extra={"api_token": "abcd1234efgh5678",
                                                                                     "note": "kept as it is"}), "sent")
        body = self.sentry.got[0]["body"]
        self.assertNotIn(b"abcd1234efgh5678", body)
        self.assertIn(b"kept as it is", body)
        self.assertFalse(os.path.exists(stray))

    def test_an_old_waiting_report_is_dropped_though_the_receiver_is_down(self):
        self.sentry.status = 503
        self.damage()
        self.updater().check(apply=True, manual=True)
        self.clock[0] += 4 * 24 * HOUR
        self.updater().check(apply=False, manual=True)
        self.assertEqual(self.read("layers", "reports.json")["pending"], [])
        self.assertEqual(len(self.sentry.got), 1)

    def test_a_restore_the_owner_asked_for_is_no_failure(self):
        base.write(os.path.join(self.codes(), "restored.json"), json.dumps(
            {"name": "1.2.3-team", "at": self.clock[0], "why": "owner", "count": 1, "paths": ["core/x.py"]}))
        self.updater().check(apply=False, manual=True)
        self.assertEqual(self.sentry.got, [])


class ShellReport(Stand):
    """A new environment that did not come up or could not be put in place reaches the developers too (the live probe
    p62, 09.11: the person was told, Sentry was not): once per environment, by its own mark."""

    def setUp(self):
        super().setUp()
        self.release("v1.3.0")   # a release on the server: the pass itself fails nothing, its one send is this report
        self.publish("v1.3.0")

    def write_up(self, sha, status="returned", reported=False, age=0):
        from core import shellnext
        shellnext.write(self.state, {"status": status, "why": "timeout", "back": True, "runtime": 3, "was_runtime": 2,
                                     "sha256": sha, "then": "1.4.0", "platform": "linux-x64", "at": self.clock[0] - age,
                                     **({"reported": True} if reported else {})})
        self.updater().check(apply=False)
        return [e for e, _ in self.sentry.events() if e["tags"]["step"] == "shell"]

    def test_a_returned_environment_is_reported_once(self):
        got = self.write_up("ab" * 32)
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["tags"]["to"], got[0]["extra"]["shell"]["then"]), ("runtime 3", "1.4.0"))
        self.assertEqual(got[0]["extra"]["shell"]["sha256"], "ab" * 32)
        self.updater().check(apply=False)
        self.assertEqual(len([e for e, _ in self.sentry.events() if e["tags"]["step"] == "shell"]), 1)

    def test_a_build_that_failed_to_be_put_in_place_and_then_was_returned_is_told_its_return(self):
        # the refused preparation is the pass's own report (bad-shell); the record's "failed" is not sent again
        self.assertEqual(self.write_up("ab" * 32, status="failed", reported=True), [])
        self.assertEqual(len(self.write_up("ab" * 32)), 1)   # the same build returned later: told

    def test_an_old_record_is_not_news(self):
        self.assertEqual(self.write_up("ab" * 32, age=8 * 86400), [])

    def test_reports_turned_on_later_still_tell_it(self):
        self.deliver(report="")
        self.assertEqual(self.write_up("ab" * 32), [])
        self.deliver(report=self.sentry.dsn)
        self.assertEqual(len(self.write_up("ab" * 32)), 1)


class Envelope(unittest.TestCase):
    def test_the_dsn_and_its_parts(self):
        from modules.update import report
        got = report.dsn("https://abc0123456789@sentry.example.test/7")
        self.assertEqual(got, {"url": "https://sentry.example.test/api/7/envelope/", "key": "abc0123456789"})
        for bad in ("http://k123456789@sentry.example.test/7", "https://sentry.example.test/7", "nonsense",
                    "https://k123456789@sentry.example.test/x", None, 5):
            self.assertIsNone(report.dsn(bad), bad)
        self.assertEqual(report.dsn("http://k123456789@127.0.0.1:9/3")["url"], "http://127.0.0.1:9/api/3/envelope/")

    def test_the_key_is_cut_wherever_it_stands(self):
        from modules.update import report
        text = 'GET https://d.example.test/v1/x?key=SeCrEtKeY-123456 Bearer SeCrEtKeY-123456 "SeCrEtKeY-123456"'
        got = report.scrub(text, "SeCrEtKeY-123456")
        self.assertNotIn("SeCrEtKeY", got)
        self.assertEqual(got, 'GET https://d.example.test/v1/x?key=[build key] Bearer [build key] "[build key]"')
        self.assertEqual(report.scrub("Authorization: Bearer abcdefghijklmnop1234", None),
                         "Authorization: Bearer [build key]")

    def test_an_envelope_is_lines_with_lengths(self):
        from modules.update import report
        body = report.envelope({"event_id": "a" * 32, "message": "x"}, {"a.log": "γραμμή\n".encode()},
                               "https://k@example.test/1")
        head, rest = body.split(b"\n", 1)
        self.assertEqual(json.loads(head)["event_id"], "a" * 32)
        item, rest = rest.split(b"\n", 1)
        n = json.loads(item)["length"]
        self.assertEqual(json.loads(rest[:n])["message"], "x")


class Guard(unittest.TestCase):
    def test_tests_never_reach_a_real_receiver(self):
        from modules.update import report
        self.assertEqual(os.environ.get("HUB_NO_REPORT"), "1")
        got = report._post({"url": "https://sentry.example.test/api/1/envelope/", "key": "k0123456789"}, b"x")
        self.assertEqual(got, "retry")


class Minimum(Stand):
    def put_min(self, version, since=None, stops="claude,bus,nonsense", reason=None):
        send = base.load("deliver_send", os.path.join(base.REPO, "tools", "package", "deliver_send.py"))
        import sys
        sys.modules["deliver_send"] = send
        self.addCleanup(sys.modules.pop, "deliver_send", None)
        args = [self.url, "--key-file", self.key_file, "min", "team", "--version", version, "--send"]
        if reason is not False:
            path = os.path.join(self.root, "reason.json")
            base.write(path, json.dumps(reason or {"en": "The sign-in changed.", "de": "Die Anmeldung hat sich geändert."}))
            args += ["--reason", path]
        if since:
            args += ["--since", since]
        if stops:
            args += ["--stops", stops]
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(send.main(args), 0)

    def minimum(self):
        return self.events("update.checked")[-1]["data"].get("min")

    def test_below_the_minimum_every_check_says_so_with_the_reason_and_what_stops(self):
        self.put_min("1.3.0")
        self.updater().check(apply=False)
        got = self.minimum()
        self.assertEqual((got["version"], got["below"]), ("1.3.0", True))
        self.assertEqual(got["reason"], {"en": "The sign-in changed.", "de": "Die Anmeldung hat sich geändert."})
        self.assertEqual(got["stops"], ["claude", "bus"])          # only what the core knows to name
        self.assertNotIn("ahead", got)

    def test_at_or_above_the_minimum_nothing_is_said(self):
        self.put_min("1.2.3")
        self.updater().check(apply=False)
        self.assertIsNone(self.minimum())

    def test_a_minimum_from_a_later_day_is_a_warning_ahead(self):
        self.put_min("1.3.0", since="2099-01-01")
        self.updater().check(apply=False)
        got = self.minimum()
        self.assertEqual((got["below"], got["ahead"], got["since"]), (True, True, "2099-01-01"))

    def test_with_updates_off_no_minimum_hangs_on(self):
        self.put_min("1.3.0")
        self.updater().check(apply=False)
        self.updater(enabled=False).check(apply=False)
        self.assertIsNone(self.minimum())

    def test_without_the_server_the_last_minimum_read_stays(self):
        self.put_min("1.3.0")
        self.updater().check(apply=False)
        self.srv.shutdown()
        self.srv.server_close()
        self.assertEqual(self.updater().check(apply=False), "offline")
        self.assertEqual(self.minimum()["version"], "1.3.0")

    def test_the_minimum_reads_without_the_build_key(self):
        self.put_min("1.3.0")
        base.write(os.path.join(self.codes(), "deliver.json"), json.dumps({"url": self.url, "key": "revoked-key-00000000"}))
        self.assertEqual(self.updater().check(apply=False), "error")
        self.assertTrue(self.minimum()["below"])

    def test_the_version_the_minimum_asks_for_is_on_offer(self):
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.put_min("1.3.0")
        self.assertEqual(self.updater().check(apply=False), "available")
        data = self.events("update.checked")[-1]["data"]
        self.assertTrue(data["min"]["below"])

    def test_a_release_below_the_minimum_is_on_offer_too(self):
        # there is no delay of a day for the app (slice A3n): any newer release is offered at once
        self.release("v1.3.0")
        self.publish("v1.3.0")
        self.put_min("1.4.0")
        self.assertEqual(self.updater().check(apply=False), "available")
        self.assertTrue(self.events("update.checked")[-1]["data"]["min"]["below"])

    def test_an_open_build_reads_its_own_entry(self):
        self.put_min("1.3.0")
        from core import appcode
        man = appcode.load(os.path.join(self.codes(), self.current()))
        self.assertEqual(man.get("flavor") or "team", "team")
        u = self.updater()
        u.running = lambda: ("1.2.3-open", {"version": "1.2.3", "flavor": "open"})
        u.check(apply=False)
        self.assertIsNone(self.minimum())


class Telegram(unittest.TestCase):
    """Below the minimum: a message with "Update" once a day while it lasts. The bot as the A3 offer test sets it up
    (borrowed, not inherited: its tests run in their own module)."""
    buttons = base.Telegram.buttons

    def low(self, top=None, **data):
        self.j.append("update.checked", None, {"result": "current", "app": True, "layer": True, "update_channel": "stable",
                                               "have_version": "1.2.3", "min": {"version": "1.3.0", "below": True,
                                               "reason": {"en": "The sign-in changed.", "de": "Die Anmeldung hat sich geändert."},
                                               "stops": ["bus"], **data}, **(top or {})}, src="update", ts=self.now)
        self.bot.tick()

    def setUp(self):
        base.Telegram.setUp(self)
        self.now = 1_800_000_000

    def test_once_a_day_while_below_with_the_button(self):
        self.low()
        self.now += 6 * HOUR
        self.low()
        got = self.buttons()
        self.assertEqual(len(got), 1)
        self.assertIn("1.3.0", got[0]["text"])
        self.assertIn("The sign-in changed.", got[0]["text"])
        self.now += 19 * HOUR
        self.low()
        self.assertEqual(len(self.buttons()), 2)

    def test_below_and_a_version_on_offer_is_one_message_and_none_while_it_installs(self):
        self.low({"result": "available", "version": "1.3.0", "notes": ["new"]})
        self.assertEqual(len(self.buttons()), 1)            # not also "version 1.3.0 is available"
        self.now += 25 * HOUR
        self.low({"result": "restarting", "version": "1.3.0"})   # pressed: it waits for the restart — no nagging
        self.assertEqual(len(self.buttons()), 1)

    def test_the_minimum_takes_the_offer_of_any_version_on_offer(self):
        # no delay of a day for the app (slice A3n): a version on offer is always due, so the minimum's message
        # carries its "Update" and the offer does not come as a second message
        self.low({"result": "available", "version": "1.2.9"})
        self.assertEqual(len(self.buttons()), 1)
        self.assertIn("1.3.0", self.buttons()[-1]["text"])
        self.assertEqual(self.bot.st["update_offered"], "1.2.9")
        self.now += HOUR
        self.low({"result": "available", "version": "1.2.9"})
        self.assertEqual(len(self.buttons()), 1)

    def test_a_warning_ahead_names_the_day_and_above_nothing_goes(self):
        self.low(ahead=True, since="2099-01-01")
        self.assertIn("2099-01-01", self.buttons()[-1]["text"])
        self.j.append("update.checked", None, {"result": "current", "app": True, "layer": True}, src="update")
        self.bot.tick()
        self.assertEqual(len(self.buttons()), 1)


class Fold(unittest.TestCase):
    def test_the_check_keeps_the_minimum_and_the_report_line(self):
        from core import reducer
        st = reducer.empty()
        ev = {"type": "update.checked", "seq": 1, "ts": 10, "entity": None, "src": "update",
              "data": {"result": "current", "layer": True, "report": {"on": False},
                       "min": {"version": "1.3.0", "below": True, "ahead": True, "since": "2099-01-01",
                               "reason": {"en": "x" * 5000, "ru": "y"}, "stops": ["bus", 5]}}}
        st, _, rejected = reducer.reduce(st, ev)
        self.assertIsNone(rejected)
        got = st["update_check"]
        self.assertEqual(got["report"], {"on": False})
        self.assertEqual(got["min"]["version"], "1.3.0")
        self.assertEqual(got["min"]["stops"], ["bus"])
        self.assertLessEqual(len(got["min"]["reason"]["en"]), 2000)


if __name__ == "__main__":
    unittest.main()
