"""The delivery server of the app's releases (deliver/server.py, app spec §4.1) and tools/package/upload.py sending to
it: reads by the build key (a header, or the key in the address for an installer link), writes only by the release
key, a published file is never replaced (other bytes — refused), big files go in parts, releases.json points only at
files that are there with those sums, min.json is open. The server runs in this process on a free port of
127.0.0.1; one test starts it as its own process, the way the image does."""
import tests  # noqa: F401 - keeps the machine's settings out
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import platforms
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from tests.fakes import cut

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER = os.path.join(ROOT, "deliver", "server.py")
UPLOAD = os.path.join(ROOT, "tools", "package", "upload.py")
SEND = os.path.join(ROOT, "tools", "package", "deliver_send.py")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def sha(data):
    return hashlib.sha256(data).hexdigest()


READ, WRITE = "read-key-for-the-test-only", "write-key-for-the-test-only"


@cut.needs_deliver()   # the open flavor cuts deliver/ (core/appcode.OPEN_CUT); the subclasses are skipped with it
class Stand(unittest.TestCase):
    """A server over a temporary data folder, keys READ (the build) and WRITE (the release)."""
    max_body = 1 << 20

    def setUp(self):
        self.server_mod = load("deliver_server", SERVER)
        self.data = tempfile.mkdtemp(prefix="deliver-")
        self.addCleanup(platforms.current.rmtree, self.data)
        self.logs = []
        self.srv = self.server_mod.make(self.data, "127.0.0.1", 0, read=[sha(READ.encode())],
                                        write=[sha(WRITE.encode())], max_body=self.max_body, say=self.logs.append)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def call(self, method, path, body=None, key=None, headers=None):
        """(status, headers, body) — an HTTP error is an answer too."""
        h = dict(headers or {})
        if key:
            h["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(self.url + path, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), e.read()

    def put_file(self, kind, name, data, offset=0, size=None, digest=None, key=WRITE, chunk=None):
        size = len(data) if size is None else size
        part = data[offset:] if chunk is None else chunk
        q = f"?offset={offset}&size={size}&sha256={digest or sha(data)}"
        return self.call("PUT", f"/v1/{kind}/{name}{q}", part, key=key)


class Reads(Stand):

    def test_health_and_min_json_are_open_the_rest_needs_the_build_key(self):
        self.assertEqual(self.call("GET", "/healthz")[0], 200)
        self.assertEqual(self.call("GET", "/min.json")[0], 404)
        mini = {"team": {"version": "1.9.0", "reason": {"en": "x"}, "since": "2026-11-12", "stops": ["bus"]}}
        self.assertEqual(self.call("PUT", "/min.json", json.dumps(mini).encode(), key=WRITE,
                                   headers={"If-None-Match": "*"})[0], 200)
        st, _, body = self.call("GET", "/min.json")
        self.assertEqual((st, json.loads(body)), (200, mini))
        self.assertEqual(self.put_file("layer", "code-1.9.0-team.tar.gz", b"layer")[0], 201)
        for key in (None, "wrong"):
            self.assertEqual(self.call("GET", "/v1/layer/code-1.9.0-team.tar.gz", key=key)[0], 401, key)
        st, _, body = self.call("GET", "/v1/layer/code-1.9.0-team.tar.gz", key=READ)
        self.assertEqual((st, body), (200, b"layer"))
        # an installer link carries the key in its address
        self.assertEqual(self.call("GET", f"/v1/layer/code-1.9.0-team.tar.gz?key={READ}")[2], b"layer")

    def test_the_key_in_an_address_never_reaches_the_log(self):
        self.put_file("app", "agents-hub-1.9.0-mac-arm64.dmg", b"dmg")
        self.call("GET", f"/v1/app/agents-hub-1.9.0-mac-arm64.dmg?key={READ}")
        self.call("GET", "/v1/app/agents-hub-1.9.0-mac-arm64.dmg?key=a-guessed-one")
        self.assertTrue(self.logs)
        self.assertFalse([line for line in self.logs if READ in line or "a-guessed-one" in line], self.logs)
        with open(os.path.join(self.data, "log.jsonl")) as f:
            self.assertNotIn(READ, f.read())

    def test_the_build_key_cannot_write(self):
        self.assertEqual(self.put_file("layer", "code-1.9.0-team.tar.gz", b"x", key=READ)[0], 403)
        self.assertEqual(self.call("PUT", "/min.json", b"{}", key=READ)[0], 403)
        self.assertEqual(self.call("PUT", "/min.json", b"{}")[0], 401)
        self.assertEqual(self.call("GET", "/v1/layer/code-1.9.0-team.tar.gz", key=READ)[0], 404)

    def test_a_broken_download_continues_with_a_range(self):
        data = bytes(range(256)) * 10
        self.put_file("layer", "code-1.9.0-team.tar.gz", data)
        st, h, body = self.call("GET", "/v1/layer/code-1.9.0-team.tar.gz", key=READ, headers={"Range": "bytes=1000-"})
        self.assertEqual((st, body), (206, data[1000:]))
        self.assertEqual(h["Content-Range"], f"bytes 1000-{len(data) - 1}/{len(data)}")
        st, _, body = self.call("HEAD", "/v1/layer/code-1.9.0-team.tar.gz", key=READ)
        self.assertEqual((st, body), (200, b""))
        self.assertEqual(self.call("GET", "/v1/layer/code-1.9.0-team.tar.gz", key=READ,
                                   headers={"Range": f"bytes={len(data)}-"})[0], 416)

    def test_names_outside_the_store_are_not_found(self):
        self.put_file("layer", "code-1.9.0-team.tar.gz", b"x")
        for path in ("/v1/layer/..%2Flog.jsonl", "/v1/layer/../log.jsonl", "/v1/layer/.code", "/v1/other/x",
                     "/v1/layer/", "/log.jsonl", "/v1/layer/code-1.9.0-team.tar.gz/x"):
            self.assertIn(self.call("GET", path, key=READ)[0], (400, 404), path)
            self.assertIn(self.call("PUT", path + "?offset=0&size=1&sha256=" + sha(b"y"), b"y", key=WRITE)[0],
                          (400, 404), path)


class Writes(Stand):
    max_body = 64

    def test_a_published_file_is_never_replaced(self):
        self.assertEqual(self.put_file("layer", "code-1.9.0-team.tar.gz", b"first")[0], 201)
        st, _, body = self.put_file("layer", "code-1.9.0-team.tar.gz", b"first")
        self.assertEqual((st, json.loads(body)["state"]), (200, "same"))
        self.assertEqual(self.put_file("layer", "code-1.9.0-team.tar.gz", b"other")[0], 409)
        self.assertEqual(self.call("GET", "/v1/layer/code-1.9.0-team.tar.gz", key=READ)[2], b"first")

    def test_a_big_file_goes_in_parts_and_is_stored_only_when_its_sum_matches(self):
        data = os.urandom(200)
        self.assertEqual(self.put_file("app", "a.dmg", data)[0], 413)       # one body over the limit
        st, _, body = self.put_file("app", "a.dmg", data, chunk=data[:64])
        self.assertEqual((st, json.loads(body)), (202, {"state": "partial", "offset": 64}))
        self.assertEqual(self.call("GET", "/v1/app/a.dmg", key=READ)[0], 404)  # not there until it is whole
        self.assertEqual(self.put_file("app", "a.dmg", data, offset=64, chunk=data[64:128])[0], 202)
        # a part again (a retry after a lost answer) — the server says where it is
        st, h, _ = self.put_file("app", "a.dmg", data, offset=64, chunk=data[64:128])
        self.assertEqual((st, h.get("X-Offset")), (409, "128"))
        # a part of another file under the same name in the middle — refused, the gathered one goes on
        st, h, _ = self.put_file("app", "a.dmg", os.urandom(200), offset=128, chunk=data[128:192])
        self.assertEqual((st, h.get("X-Offset")), (409, "0"))
        self.assertEqual(self.put_file("app", "a.dmg", data, offset=128, chunk=data[128:192])[0], 202)
        self.assertEqual(self.put_file("app", "a.dmg", data, offset=192, chunk=data[192:])[0], 201)
        self.assertEqual(self.call("GET", "/v1/app/a.dmg", key=READ)[2], data)
        self.assertEqual(os.listdir(os.path.join(self.data, "uploads")), [])     # no part left behind
        # bytes that do not give the announced sum are dropped, nothing is stored
        bad = os.urandom(10)
        self.assertEqual(self.put_file("app", "b.dmg", bad, digest=sha(b"something else"))[0], 422)
        self.assertEqual(self.call("GET", "/v1/app/b.dmg", key=READ)[0], 404)
        self.assertEqual(self.put_file("app", "b.dmg", bad)[0], 201)
        # past the announced size
        self.assertEqual(self.put_file("app", "c.dmg", b"abc", size=2)[0], 400)

    def test_releases_json_points_only_at_stored_files_and_changes_only_from_what_was_read(self):
        data = b"layer bytes"
        doc = {"channels": {"stable": {"version": "1.9.0", "commit": "ab" * 20, "published_at": "2026-11-07T10:00:00Z",
                                       "runtime_min": 3, "notes": "x",
                                       "layer": {"url": "/v1/layer/code-1.9.0-team.tar.gz", "sha256": sha(data),
                                                 "size": len(data)}}},
               "runtime": {"current": 3, "apps": {}}}
        body = json.dumps(doc).encode()
        new = {"If-None-Match": "*"}
        self.assertEqual(self.call("PUT", "/v1/releases.json", body, key=WRITE, headers=new)[0], 400)  # no file yet
        self.put_file("layer", "code-1.9.0-team.tar.gz", data)
        self.assertEqual(self.call("PUT", "/v1/releases.json", body, key=WRITE)[0], 428)  # neither If-Match nor *
        st, h, _ = self.call("PUT", "/v1/releases.json", body, key=WRITE, headers=new)
        self.assertEqual(st, 200)
        etag = h["ETag"]
        self.assertEqual(self.call("PUT", "/v1/releases.json", body, key=WRITE, headers=new)[0], 412)
        st, h, got = self.call("GET", "/v1/releases.json", key=WRITE)   # the writer reads the document as stored
        self.assertEqual((st, json.loads(got), h["ETag"]), (200, doc, etag))
        st, h, got = self.call("GET", "/v1/releases.json", key=READ)    # a hub reads it with the count of hubs added
        self.assertEqual((st, json.loads(got)["channels"]["stable"]["stands"]), (200, 0))
        wrong = json.loads(body)
        wrong["channels"]["stable"]["layer"]["sha256"] = sha(b"other")
        self.assertEqual(self.call("PUT", "/v1/releases.json", json.dumps(wrong).encode(), key=WRITE,
                                   headers={"If-Match": etag})[0], 400)
        self.assertEqual(self.call("PUT", "/v1/releases.json", b"not json", key=WRITE,
                                   headers={"If-Match": etag})[0], 400)
        self.assertEqual(self.call("PUT", "/v1/releases.json", body, key=WRITE, headers={"If-Match": '"0"'})[0], 412)
        self.assertEqual(self.call("PUT", "/v1/releases.json", body, key=WRITE, headers={"If-Match": etag})[0], 200)

    def test_min_json_has_a_form(self):
        for bad in (b"[]", b"{", json.dumps({"team": {"since": "x"}}).encode(),
                    json.dumps({"other": {"version": "1"}}).encode()):
            self.assertEqual(self.call("PUT", "/min.json", bad, key=WRITE, headers={"If-None-Match": "*"})[0], 400, bad)

    def test_documents_change_only_from_the_version_read_whatever_a_proxy_does_to_the_tag(self):
        one, two = json.dumps({"team": {"version": "1"}}).encode(), json.dumps({"team": {"version": "2"}}).encode()
        for headers in ({}, {"If-None-Match": '"abc"'}):       # no precondition, or one that names no version
            self.assertEqual(self.call("PUT", "/min.json", one, key=WRITE, headers=headers)[0], 428, headers)
        self.assertEqual(self.call("PUT", "/min.json", one, key=WRITE, headers={"If-None-Match": "*"})[0], 200)
        st, h, _ = self.call("GET", "/min.json")
        self.assertEqual(h["X-Sha256"], sha(one))
        self.assertIn("no-transform", h["Cache-Control"])
        self.assertEqual(self.call("PUT", "/min.json", two, key=WRITE, headers={"If-None-Match": "*"})[0], 412)
        # a proxy that compressed the answer hands on a weak tag; the sum is the same
        self.assertEqual(self.call("PUT", "/min.json", two, key=WRITE, headers={"If-Match": f'W/"{sha(one)}"'})[0],
                         200)
        self.assertEqual(self.call("PUT", "/min.json", one, key=WRITE, headers={"If-Match": f'"{sha(one)}"'})[0], 412)
        self.assertEqual(self.call("PUT", "/min.json", one, key=WRITE, headers={"If-Match": f'"0", "{sha(two)}"'})[0],
                         200)                                    # a list of tags: any of them
        self.assertEqual(self.call("PUT", "/min.json", two, key=WRITE, headers={"If-Match": "*"})[0], 200)

    def test_an_answer_to_a_put_never_leaves_its_body_for_the_next_request(self):
        import http.client
        data = b"x" * 60
        self.assertEqual(self.put_file("layer", "code-1.9.0-team.tar.gz", data)[0], 201)
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        q = f"/v1/layer/code-1.9.0-team.tar.gz?offset=0&size={len(data)}&sha256={sha(data)}"
        for want in (200, 200, 409):          # the same twice on one connection, then other bytes
            body = data if want == 200 else b"y" * 60
            digest = sha(body)
            conn.request("PUT", q.replace(sha(data), digest), body, {"Authorization": f"Bearer {WRITE}"})
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, want)
        conn.request("GET", "/v1/layer/code-1.9.0-team.tar.gz", headers={"Authorization": f"Bearer {READ}"})
        r = conn.getresponse()
        self.assertEqual((r.status, r.read()), (200, data))

    def test_a_put_refused_before_its_key_is_known_closes_the_connection(self):
        import http.client
        data = b"x" * 60
        q = f"/v1/layer/code-1.9.0-team.tar.gz?offset=0&size={len(data)}&sha256={sha(data)}"
        for key in (None, "nope", READ):       # nobody, a wrong key, a key that cannot write
            h = {"Authorization": f"Bearer {key}"} if key else {}
            conn = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=10)
            self.addCleanup(conn.close)
            conn.request("PUT", q, data, h)
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 403 if key == READ else 401)
            self.assertEqual(r.getheader("Connection"), "close")     # a proxy would reuse it for someone else
            conn.request("GET", "/healthz")
            r = conn.getresponse()
            self.assertEqual((r.status, r.read()), (200, b"ok"))

    def test_a_file_published_without_its_meta_gets_it_back(self):
        self.put_file("layer", "code-1.9.0-team.tar.gz", b"abc")
        os.unlink(os.path.join(self.data, "meta", "layer", "code-1.9.0-team.tar.gz.json"))   # stopped in between
        self.assertEqual(json.loads(self.put_file("layer", "code-1.9.0-team.tar.gz", b"abc")[2])["state"], "same")
        st, h, _ = self.call("HEAD", "/v1/layer/code-1.9.0-team.tar.gz", key=READ)
        self.assertEqual(h["ETag"], f'"{sha(b"abc")}"')

    def test_a_name_with_a_plus_goes_quoted_or_not(self):
        self.assertEqual(self.put_file("layer", "code-1.9.0%2B1-team.tar.gz", b"p")[0], 201)
        self.assertEqual(self.call("GET", "/v1/layer/code-1.9.0+1-team.tar.gz", key=READ)[2], b"p")

    def test_writes_are_logged(self):
        self.put_file("layer", "code-1.9.0-team.tar.gz", b"x")
        self.put_file("layer", "code-1.9.0-team.tar.gz", b"y")
        with open(os.path.join(self.data, "log.jsonl")) as f:
            lines = [json.loads(x) for x in f]
        self.assertEqual([(x["path"], x["result"]) for x in lines],
                         [("/v1/layer/code-1.9.0-team.tar.gz", "stored"), ("/v1/layer/code-1.9.0-team.tar.gz", "clash")])
        self.assertEqual(lines[0]["sha256"], sha(b"x"))


class Stands(Stand):
    """"Stands on <release> at N hubs" (app spec §4.1, slice A3n): a hub that came up on a release says so once with its
    own random id; the server keeps id -> release (a repeat is one hub, a hub that went back says the other release or
    nothing) and hands the number of each channel's release to the readers in releases.json."""

    def setUp(self):
        super().setUp()
        self.now = [1_800_000_000.0]
        self.srv.store.stands.clock = lambda: self.now[0]
        data = b"layer bytes"
        self.put_file("layer", "code-1.9.0-team.tar.gz", data)
        self.put_file("layer", "code-1.8.0-team.tar.gz", data + b"!")
        entry = lambda v, extra: {"version": v, "layer": {"url": f"/v1/layer/code-{v}-team.tar.gz", "sha256": sha(extra),
                                                         "size": len(extra)}}
        doc = {"channels": {"stable": entry("1.9.0", data), "nightly": entry("1.8.0", data + b"!")}}
        self.assertEqual(self.call("PUT", "/v1/releases.json", json.dumps(doc).encode(), key=WRITE,
                                   headers={"If-None-Match": "*"})[0], 200)

    def say(self, hub, version, key=READ):
        return self.call("POST", "/v1/stand", json.dumps({"id": hub * 32, "version": version}).encode(), key=key)

    def counts(self):
        st, _, body = self.call("GET", "/v1/releases.json", key=READ)
        self.assertEqual(st, 200)
        return {k: v.get("stands") for k, v in json.loads(body)["channels"].items()}

    def test_a_hub_is_counted_once_on_its_release_and_a_repeat_moves_nothing(self):
        self.assertEqual(self.counts(), {"stable": 0, "nightly": 0})
        self.assertEqual(self.say("a", "1.9.0")[0], 200)
        self.assertEqual(self.say("a", "1.9.0")[0], 200)          # the same hub again: still one
        self.assertEqual(self.say("b", "1.9.0")[0], 200)
        self.assertEqual(self.say("c", "1.8.0")[0], 200)          # another release: the nightly's, not the stable's
        self.assertEqual(self.say("d", "1.7.0")[0], 200)          # a release the channels do not name: counted nowhere
        self.assertEqual(self.counts(), {"stable": 2, "nightly": 1})

    def test_a_hub_that_went_back_or_left_is_taken_off_the_release(self):
        self.say("a", "1.9.0")
        self.say("b", "1.9.0")
        self.say("a", "1.8.0")                                     # a went back to the earlier one
        self.assertEqual(self.counts(), {"stable": 1, "nightly": 1})
        st, _, body = self.say("b", None)                          # b says it stands on nothing (the owner's return)
        self.assertEqual((st, json.loads(body)["state"]), (200, "removed"))
        self.assertEqual(self.say("e", None)[0], 200)             # a hub never counted: nothing to take off
        self.assertEqual(self.counts(), {"stable": 0, "nightly": 1})

    def test_the_writer_reads_the_document_as_it_stored_it(self):
        self.say("a", "1.9.0")
        st, h, body = self.call("GET", "/v1/releases.json", key=WRITE)
        self.assertEqual(st, 200)
        self.assertNotIn("stands", json.dumps(json.loads(body)))
        self.assertEqual(h["X-Sha256"], sha(body))                 # the sum is of what the writer got: it writes back from it
        st, _, again = self.call("PUT", "/v1/releases.json", body, key=WRITE, headers={"If-Match": h["ETag"]})
        self.assertEqual(st, 200, again)
        self.assertEqual(self.counts()["stable"], 1)               # the count is not part of the document: it stayed

    def test_a_hub_not_heard_from_for_a_month_is_not_counted(self):
        self.say("a", "1.9.0")
        self.now[0] += 20 * 86400
        self.say("b", "1.9.0")
        self.assertEqual(self.counts()["stable"], 2)
        self.now[0] += 11 * 86400                                  # a's word is 31 days old, b's 11
        self.assertEqual(self.counts()["stable"], 1)
        self.say("a", "1.9.0")                                     # it says so again: back
        self.assertEqual(self.counts()["stable"], 2)

    def test_the_counts_survive_a_restart_of_the_server(self):
        self.say("a", "1.9.0")
        srv = self.server_mod.make(self.data, "127.0.0.1", 0, read=[sha(READ.encode())], write=[sha(WRITE.encode())],
                                   say=lambda line: None)
        self.addCleanup(srv.server_close)
        srv.store.stands.clock = lambda: self.now[0]
        self.assertEqual(srv.store.stands.count("1.9.0"), 1)

    def test_only_the_build_or_release_key_may_say_and_the_form_is_checked(self):
        self.assertEqual(self.say("a", "1.9.0", key=None)[0], 401)
        self.assertEqual(self.say("a", "1.9.0", key="wrong")[0], 401)
        self.assertEqual(self.say("a", "1.9.0", key=WRITE)[0], 200)
        good = {"id": "a" * 32, "version": "1.9.0"}
        for bad in (b"[]", b"{", b"", json.dumps({**good, "id": "short"}).encode(),
                    json.dumps({**good, "id": "G" * 32}).encode(), json.dumps({**good, "version": "../x"}).encode(),
                    json.dumps({**good, "version": 5}).encode(), json.dumps({"id": "a" * 32}).encode(),
                    json.dumps({**good, "more": 1}).encode()):
            self.assertEqual(self.call("POST", "/v1/stand", bad, key=READ)[0], 400, bad)
        self.assertEqual(self.call("POST", "/v1/stand", b"x" * 9000, key=READ)[0], 413)
        self.assertEqual(self.call("POST", "/v1/other", b"{}", key=READ)[0], 404)
        self.assertEqual(self.call("GET", "/v1/stand", key=READ)[0], 405)      # nobody reads the list
        self.assertEqual(self.counts()["stable"], 1)

    def test_a_full_list_takes_no_new_hub_but_lets_the_known_ones_move(self):
        self.srv.store.stands.limit = 2
        self.say("a", "1.9.0")
        self.say("b", "1.9.0")
        self.assertEqual(self.say("c", "1.9.0")[0], 507)
        self.assertEqual(self.say("a", "1.8.0")[0], 200)
        self.now[0] += 31 * 86400                                  # the old words go, there is room again
        self.assertEqual(self.say("c", "1.9.0")[0], 200)

    def test_a_disk_that_refuses_changes_nothing_and_is_answered(self):
        self.say("a", "1.9.0")
        real = self.server_mod.write_atomic

        def full(path, data):
            raise OSError(28, "no space left")
        self.server_mod.write_atomic = full
        try:
            self.assertEqual(self.say("b", "1.9.0")[0], 503)
            self.assertEqual(self.say("a", None)[0], 503)
        finally:
            self.server_mod.write_atomic = real
        self.assertEqual(self.counts()["stable"], 1)             # memory and disk agree: only a
        self.assertEqual(self.say("b", "1.9.0")[0], 200)
        self.assertEqual(self.counts()["stable"], 2)

    def test_a_stored_document_of_another_form_is_served_as_stored(self):
        for odd in (b'{"channels": {"stable": null, "nightly": []}}', b'{"channels": []}', b"[1]", b"not json"):
            with open(os.path.join(self.data, "files", "releases.json"), "wb") as f:
                f.write(odd)
            st, _, body = self.call("GET", "/v1/releases.json", key=READ)
            self.assertEqual((st, body), (200, odd))

    def test_the_hubs_ids_are_neither_logged_nor_kept(self):
        self.say("a", "1.9.0")
        with open(os.path.join(self.data, "stands.json")) as f:
            kept = f.read()
        self.assertNotIn("a" * 32, kept)                           # the sum of the id, not the id
        self.assertFalse([line for line in self.logs if "a" * 32 in line])
        with open(os.path.join(self.data, "log.jsonl")) as f:
            self.assertNotIn("a" * 32, f.read())


class Pool:
    """A proxy the way Cloudflare keeps the side towards the origin: one connection to the server that every client's
    request goes over, one after another, until the server says Connection: close (or drops it)."""

    def __init__(self, port):
        import http.client
        import http.server
        self.up = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        self.lock = threading.Lock()
        pool = self

        class Hop(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else None
                head = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "connection")}
                with pool.lock:
                    try:
                        pool.up.request(self.command, self.path, body, head)
                        r = pool.up.getresponse()
                        data = r.read()
                    except Exception as e:      # the origin broke the pooled connection: what a client of CF would see
                        pool.up.close()
                        data, status, items = repr(e).encode(), 502, [("Content-Length", str(len(repr(e))))]
                    else:
                        status, items = r.status, [(k, v) for k, v in r.getheaders()
                                                   if k.lower() not in ("connection", "transfer-encoding", "server", "date")]
                self.send_response(status)
                for k, v in items:
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_PUT = do_HEAD = go

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Hop)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.up.close()


SH = b"#!/bin/sh\nmain() { echo hi; }\nmain \"$@\"\n"
PS = b"function main { 'hi' }\nmain\n"
HERE = "@@HUB_DELIVER_KEY@@"


class Install(Stand):
    """The one-line install (one-line spec §3.3): GET /i/<key>/install.sh|ps1 gives the current script with the build
    key written into it; scripts and environments (runtime.shells) are published like the other files."""

    def publish(self, ext, version, data, point=True):
        """The script file, then releases.json naming it (as upload.py does)."""
        name = f"install-{version}.{ext}"
        st = self.put_file("script", name, data)[0]
        self.assertIn(st, (200, 201), name)
        if point:
            self.release(scripts={f"install.{ext}": {"url": f"/v1/script/{name}", "sha256": sha(data),
                                                     "size": len(data), "version": version}})

    def release(self, **sections):
        st, h, body = self.call("GET", "/v1/releases.json", key=WRITE)
        doc = json.loads(body) if st == 200 else {}
        doc["scripts"] = {**doc.get("scripts", {}), **sections.pop("scripts", {})}
        doc.update(sections)
        head = {"If-Match": h["ETag"]} if st == 200 else {"If-None-Match": "*"}
        got = self.call("PUT", "/v1/releases.json", json.dumps(doc).encode(), key=WRITE, headers=head)
        self.assertEqual(got[0], 200, got)

    def test_the_current_script_comes_with_the_build_key_written_in_as_text(self):
        self.publish("sh", "1.0.0", SH)
        self.publish("ps1", "1.0.0", PS)
        st, h, body = self.call("GET", f"/i/{READ}/install.sh")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Type"], "text/plain; charset=utf-8")     # without it irm in PowerShell 5.1 reads Latin-1
        self.assertEqual(h["Cache-Control"], "no-store, private")
        # the key is the first line (after a shebang, so the file still runs by itself), the script as it was after it
        self.assertEqual(body, b"#!/bin/sh\nHUB_DELIVER_KEY=" + READ.encode() + b"\nmain() { echo hi; }\nmain \"$@\"\n")
        st, h, body = self.call("GET", f"/i/{READ}/install.ps1")
        self.assertEqual((st, body), (200, b"$HUB_DELIVER_KEY = '" + READ.encode() + b"'\n" + PS))
        self.assertEqual(h["Content-Type"], "text/plain; charset=utf-8")
        head = self.call("HEAD", f"/i/{READ}/install.sh")
        self.assertEqual((head[0], head[2]), (200, b""))

    def test_a_script_that_marks_the_place_of_the_key_gets_it_there_and_nothing_before(self):
        self.publish("ps1", "1.0.0", b"param([string]$Channel)\n$k = '" + HERE.encode() + b"'\n$again = \"" + HERE.encode() + b"\"\n")
        body = self.call("GET", f"/i/{READ}/install.ps1")[2]
        self.assertEqual(body, f"param([string]$Channel)\n$k = '{READ}'\n$again = \"{READ}\"\n".encode())

    def test_every_other_ask_is_a_404_without_a_body_and_without_a_hint(self):
        self.publish("sh", "1.0.0", SH)
        for path in (f"/i/wrong-key/install.sh", f"/i/{WRITE}/install.sh",       # a key it does not know, the release key
                     f"/i/{READ}/install.ps1",                                   # no such script published
                     f"/i/{READ}/install.zsh", f"/i/{READ}/", f"/i/{READ}", "/i//install.sh", "/i/",
                     f"/i/{READ}/install.sh/more", f"/i/{READ}/../install.sh", f"/i/{READ[:5]}%00/install.sh",
                     f"/i/{'k' * 300}/install.sh"):
            st, h, body = self.call("GET", path)
            self.assertEqual((st, body, h["Content-Length"]), (404, b"", "0"), path)
        for method in ("PUT", "DELETE", "POST"):
            self.assertIn(self.call(method, f"/i/{READ}/install.sh", b"x", key=WRITE)[0], (404, 405), method)
        # the key from the header does not open an address that has none; the key in a query does not either
        self.assertEqual(self.call("GET", "/i/x/install.sh", key=READ)[0], 404)
        self.assertEqual(self.call("GET", f"/i/x/install.sh?key={READ}")[0], 404)
        self.assertEqual(self.call("GET", f"/i/{READ}/install.sh?x=1")[0], 200)

    def test_the_key_of_an_install_address_is_never_logged_not_even_a_wrong_one(self):
        self.publish("sh", "1.0.0", SH)
        guessed = "a-guessed-install-key"
        for path in (f"/i/{READ}/install.sh", f"/i/{guessed}/install.sh", f"/i/{READ}/install.zsh",
                     f"//i/{READ}/install.sh", f"/i/{READ}%2Finstall.sh", f"/I/{READ}/install.sh",
                     f"/%69/{READ}/install.sh", f"/i/{READ}/install.sh?key={READ}", f"/deliver/i/{READ}/install.sh",
                     f"/./i/{READ}/install.sh", f"/x//i/{READ}", f"/v1/layer/i/{READ}"):
            self.call("GET", path)
        self.call("PUT", f"/i/{READ}/install.sh", b"x", key=WRITE)
        self.assertTrue(self.logs)
        self.assertFalse([line for line in self.logs if READ in line or guessed in line], self.logs)
        self.assertTrue([line for line in self.logs if line.startswith("GET /i/") and line.endswith(" 200")], self.logs)
        with open(os.path.join(self.data, "log.jsonl")) as f:
            self.assertNotIn(READ, f.read())

    def test_scripts_are_files_of_their_own_with_a_name_a_form_and_no_replacement(self):
        self.assertEqual(self.put_file("script", "install-1.0.0.sh", SH)[0], 201)
        self.assertEqual(self.put_file("script", "install-1.0.0.sh", SH)[2], b'{"state": "same"}')
        self.assertEqual(self.put_file("script", "install-1.0.0.sh", SH + b"#")[0], 409)
        self.assertEqual(self.put_file("script", "install-1.0.1.sh", SH, key=READ)[0], 403)
        self.assertEqual(self.call("GET", "/v1/script/install-1.0.0.sh", key=READ)[2], SH)      # the file as it is stored
        for name in ("install.sh", "x-1.0.0.sh", "install-1.0.0.bat", "install-.sh", "install-1.0.0.sh.part",
                     "install-1.0.0.ps1.want"):
            self.assertEqual(self.put_file("script", name, SH)[0], 404, name)
        for bad, why in ((b"caf\xc3\xa9\n", "non-ASCII"), (b"echo \x00\n", "NUL"), (b"", "empty")):
            st = self.put_file("script", "install-2.0.0.sh", bad)[0] if bad else \
                self.call("PUT", f"/v1/script/install-2.0.0.sh?offset=0&size=0&sha256={sha(b'')}", b"", key=WRITE)[0]
            self.assertIn(st, (400, 422), why)
            self.assertEqual(self.call("GET", "/v1/script/install-2.0.0.sh", key=READ)[0], 404, why)
        big = b"#" * ((256 << 10) + 1)
        self.assertEqual(self.put_file("script", "install-3.0.0.sh", big)[0], 413)
        self.assertEqual(self.call("GET", "/v1/script/install-3.0.0.sh", key=READ)[0], 404)
        with open(os.path.join(self.data, "log.jsonl")) as f:
            self.assertIn("/v1/script/install-1.0.0.sh", f.read())

    def test_releases_json_names_scripts_and_environments_only_as_they_are_stored(self):
        data = b"env"
        self.publish("sh", "1.0.0", SH, point=False)
        ref = {"url": "/v1/script/install-1.0.0.sh", "sha256": sha(SH), "size": len(SH), "version": "1.0.0"}
        env = {"url": "/v1/app/agents-hub-1.0.0-mac-x64.tar.gz", "sha256": sha(data), "size": len(data), "runtime": 1}

        def put(doc):
            st, h, _ = self.call("GET", "/v1/releases.json", key=WRITE)
            head = {"If-Match": h["ETag"]} if st == 200 else {"If-None-Match": "*"}
            return self.call("PUT", "/v1/releases.json", json.dumps(doc).encode(), key=WRITE, headers=head)[0]
        self.assertEqual(put({"scripts": {"install.sh": {**ref, "sha256": sha(b"other")}}}), 400)     # not what is stored
        self.assertEqual(put({"scripts": {"install.sh": {**ref, "url": "/v1/script/install-9.9.9.sh"}}}), 400)
        self.assertEqual(put({"scripts": {"install.zsh": ref}}), 400)                                  # not a script of ours
        self.assertEqual(put({"scripts": [ref]}), 400)
        self.assertEqual(put({"scripts": {"install.sh": ref}}), 200)
        # an environment is a file of the app kind; its platform is one of the six
        self.assertEqual(put({"scripts": {"install.sh": ref}, "runtime": {"current": 1, "shells": {"mac-x64": env}}}), 400)
        self.put_file("app", "agents-hub-1.0.0-mac-x64.tar.gz", data)
        for bad in ({"mac-x64": {**env, "runtime": 0}}, {"mac-x64": {**env, "runtime": "1"}}, {"mac-ppc": env}, [env],
                    {"mac-x64": {**env, "runtime": True}}):
            self.assertEqual(put({"scripts": {"install.sh": ref}, "runtime": {"current": 1, "shells": bad}}), 400, bad)
        for platform in ("mac-arm64", "mac-x64", "linux-x64", "linux-arm64", "windows-x64", "windows-arm64"):
            one = {**env, "url": f"/v1/app/agents-hub-1.0.0-{platform}.tar.gz"}
            self.put_file("app", f"agents-hub-1.0.0-{platform}.tar.gz", data)
            self.assertEqual(put({"runtime": {"current": 1, "shells": {platform: one}}}), 200, platform)
        # the archive of another platform under this one's name is a mistake the server sees too
        self.assertEqual(put({"runtime": {"current": 1, "shells": {"linux-x64": {**env, "url": "/v1/app/agents-hub-1.0.0-mac-arm64.tar.gz"}}}}), 400)

    def test_the_script_served_is_the_one_releases_json_names_now(self):
        self.publish("sh", "1.0.0", SH)
        newer = SH + b"echo more\n"
        self.publish("sh", "1.1.0", newer, point=False)
        self.assertIn(b"echo hi", self.call("GET", f"/i/{READ}/install.sh")[2])
        self.assertNotIn(b"more", self.call("GET", f"/i/{READ}/install.sh")[2])      # published is not yet current
        self.publish("sh", "1.1.0", newer)
        self.assertIn(b"echo more", self.call("GET", f"/i/{READ}/install.sh")[2])
        self.assertEqual(self.call("GET", "/v1/script/install-1.0.0.sh", key=READ)[2], SH)   # the old one stays

    def test_a_key_that_is_in_both_lists_is_not_written_into_a_script(self):
        self.srv.write.append(sha(READ.encode()))      # a misplaced setting: the release key must never go into a script
        self.publish("sh", "1.0.0", SH)
        self.assertEqual(self.call("GET", f"/i/{READ}/install.sh")[0], 404)

    def test_a_series_over_one_pooled_connection_never_trips_on_a_left_body(self):
        import http.client
        proxy = Pool(self.srv.server_address[1])
        self.addCleanup(proxy.close)
        conn = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=10)      # one keep-alive client connection
        self.addCleanup(conn.close)
        put = f"/v1/script/install-1.0.0.sh?offset=0&size={len(SH)}&sha256={sha(SH)}"
        other = b"echo other\n"
        w = {"Authorization": f"Bearer {WRITE}"}
        doc = json.dumps({"scripts": {"install.sh": {"url": "/v1/script/install-1.0.0.sh", "sha256": sha(SH),
                                                     "size": len(SH), "version": "1.0.0"}}}).encode()
        steps = [
            ("GET", f"/i/{READ}/install.sh", None, {}, 404),            # nothing published yet
            ("PUT", put, SH, {}, 401),                                   # refused before its key is known
            ("GET", f"/i/nope/install.sh", None, {}, 404),
            ("PUT", put, SH, w, 201),
            ("PUT", put, SH, w, 200),                                    # the same again
            ("PUT", put.replace(sha(SH), sha(other)), other, w, 409),    # other bytes under the name
            ("PUT", "/v1/releases.json", doc, {**w, "If-None-Match": "*"}, 200),
            ("GET", f"/i/{READ}/install.sh", None, {}, 200),
            ("HEAD", f"/i/{READ}/install.ps1", None, {}, 404),
            ("PUT", "/v1/script/install-2.0.0.sh?offset=0&size=3&sha256=" + sha(b"\xc3\xa9\n"), b"\xc3\xa9\n", w, 422),
            ("PUT", "/v1/script/install-1.0.0.sh?offset=0&size=1", SH, w, 400),
            ("GET", f"/i/{READ}/install.sh", None, {}, 200),
        ]
        for method, path, body, head, want in steps:
            conn.request(method, path, body, head)
            r = conn.getresponse()
            data = r.read()
            self.assertEqual(r.status, want, (method, path, data))
        self.assertIn(b"HUB_DELIVER_KEY=" + READ.encode(), data)       # the last answer is still a whole script
        self.assertFalse([line for line in self.logs if READ in line], self.logs)


@cut.needs_deliver()
class Process(unittest.TestCase):
    """`server.py serve` as the image runs it: settings from the environment, the keys only as their sums."""

    def test_serve_from_the_environment(self):
        data = tempfile.mkdtemp(prefix="deliver-")
        self.addCleanup(platforms.current.rmtree, data)
        env = {**os.environ, "HUB_DELIVER_DATA": data, "HUB_DELIVER_HOST": "127.0.0.1", "HUB_DELIVER_PORT": "0",
               "HUB_DELIVER_READ_KEYS": sha(READ.encode()), "HUB_DELIVER_WRITE_KEYS": sha(WRITE.encode())}
        p = subprocess.Popen([sys.executable, SERVER, "serve"], env=env, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.terminate)
        line = p.stdout.readline().decode()
        self.assertIn("listening", line)
        url = "http://" + line.split()[-1]
        req = urllib.request.Request(url + "/v1/releases.json", headers={"Authorization": f"Bearer {READ}"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        e.exception.close()
        self.assertEqual(e.exception.code, 404)          # the key is taken, nothing is published yet
        with urllib.request.urlopen(url + "/healthz", timeout=10) as r:
            self.assertEqual(r.read(), b"ok")

    def test_without_keys_it_does_not_start(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_DELIVER_")}
        r = subprocess.run([sys.executable, SERVER, "serve"], env={**env, "HUB_DELIVER_DATA": tempfile.gettempdir()},
                           capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HUB_DELIVER_READ_KEYS", r.stderr)

    def test_keygen_prints_a_key_and_its_sum(self):
        r = subprocess.run([sys.executable, SERVER, "keygen"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        key = r.stdout.strip()
        self.assertGreaterEqual(len(key), 40)
        self.assertIn(sha(key.encode()), r.stderr)


class Upload(Stand):
    """upload.py <server> …: a look by default, sending with --send; the release key from a 0600 file."""
    max_body = 64

    def setUp(self):
        super().setUp()
        self.up_mod = load("deliver_upload", UPLOAD)
        send = sys.modules["deliver_send"] = load("deliver_send", SEND)   # what upload.py imports for a server
        self.addCleanup(sys.modules.pop, "deliver_send", None)
        send.CHUNK = 64
        self.dir = tempfile.mkdtemp(prefix="hub-upload-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.key = os.path.join(self.dir, "release.key")
        with open(self.key, "w") as f:
            f.write(WRITE + "\n")
        os.chmod(self.key, 0o600)
        self.layer = os.path.join(self.dir, "code-1.9.0-team.tar.gz")
        self.bytes = os.urandom(150)
        with open(self.layer, "wb") as f:
            f.write(self.bytes)

    def up(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.up_mod.main([self.url, *args, "--key-file", self.key])
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def releases(self):
        st, _, body = self.call("GET", "/v1/releases.json", key=READ)
        return json.loads(body) if st == 200 else None

    def layer_args(self, version="1.9.0", channel="stable"):
        return ["layer", self.layer, "--channel", channel, "--version", version, "--commit", "cd" * 20,
                "--runtime-min", "3"]

    def test_without_send_it_only_looks(self):
        code, out, err = self.up(*self.layer_args())
        self.assertEqual(code, 0, err)
        self.assertIn(sha(self.bytes), out)
        self.assertIsNone(self.releases())
        self.assertEqual(self.call("GET", "/v1/layer/code-1.9.0-team.tar.gz", key=READ)[0], 404)

    def test_send_stores_the_layer_in_parts_and_points_the_channel_at_it(self):
        code, out, err = self.up(*self.layer_args(), "--send")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.call("GET", "/v1/layer/code-1.9.0-team.tar.gz", key=READ)[2], self.bytes)
        st = self.releases()["channels"]["stable"]
        self.assertEqual(st["layer"], {"url": "/v1/layer/code-1.9.0-team.tar.gz", "sha256": sha(self.bytes),
                                       "size": len(self.bytes)})
        self.assertEqual((st["version"], st["commit"], st["runtime_min"]), ("1.9.0", "cd" * 20, 3))
        # the same again: nothing new, still fine; another channel keeps the first
        self.assertEqual(self.up(*self.layer_args(), "--send")[0], 0)
        nightly = os.path.join(self.dir, "code-1.9.0-1107-team.tar.gz")
        with open(nightly, "wb") as f:
            f.write(b"night")
        args = self.layer_args("1.9.0-1107", "nightly")
        args[1] = nightly
        self.assertEqual(self.up(*args, "--send")[0], 0)
        self.assertEqual(set(self.releases()["channels"]), {"stable", "nightly"})

    def test_other_bytes_under_a_published_name_are_refused(self):
        self.assertEqual(self.up(*self.layer_args(), "--send")[0], 0)
        with open(self.layer, "wb") as f:
            f.write(b"changed")
        code, out, err = self.up(*self.layer_args(), "--send")
        self.assertEqual(code, 3, err + out)
        self.assertEqual(code, self.up(*self.layer_args())[0])     # the look already says so
        self.assertEqual(self.releases()["channels"]["stable"]["layer"]["sha256"], sha(self.bytes))

    def test_the_client_names_files_as_the_server_does(self):
        send = sys.modules["deliver_send"]
        self.assertEqual((send.NAME.pattern, send.VERSION.pattern),
                         (self.server_mod.NAME.pattern, self.server_mod.VERSION.pattern))
        self.assertEqual((send.SHELL_PLATFORMS, send.SCRIPTS, send.MAX_SCRIPT, send.SCRIPT_TEXT.pattern),
                         (self.server_mod.PLATFORMS, self.server_mod.SCRIPTS, self.server_mod.MAX_SCRIPT,
                          self.server_mod.SCRIPT_TEXT.pattern))

    def test_a_version_with_a_plus(self):
        plus = os.path.join(self.dir, "code-1.9.0+1-team.tar.gz")
        os.rename(self.layer, plus)
        args = self.layer_args("1.9.0+1")
        args[1] = plus
        code, out, err = self.up(*args, "--send")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.releases()["channels"]["stable"]["version"], "1.9.0+1")

    def test_a_file_not_named_for_its_version_is_refused(self):
        self.assertEqual(self.up(*self.layer_args("1.9.1"), "--send")[0], 2)

    def test_the_key_file_must_be_private(self):
        os.chmod(self.key, 0o644)
        if os.name == "nt":  # portable: the mode bits are POSIX
            self.skipTest("mode bits")
        self.assertEqual(self.up(*self.layer_args(), "--send")[0], 2)

    def test_an_app_and_min_json(self):
        dmg = os.path.join(self.dir, "agents-hub-1.9.0-mac-arm64.dmg")
        with open(dmg, "wb") as f:
            f.write(b"dmg")
        self.assertEqual(self.up("app", dmg, "--platform", "mac-arm64", "--runtime", "4", "--send")[0], 0)
        rt = self.releases()["runtime"]
        self.assertEqual(rt["current"], 4)
        self.assertEqual(rt["apps"]["mac-arm64"], {"url": "/v1/app/agents-hub-1.9.0-mac-arm64.dmg",
                                                   "sha256": sha(b"dmg"), "size": 3, "runtime": 4})
        reason = os.path.join(self.dir, "reason.json")
        with open(reason, "w") as f:
            json.dump({"en": "the bus changed", "ru": "x"}, f)
        code, _, err = self.up("min", "team", "--version", "1.9.0", "--reason", reason, "--since", "2026-11-12",
                               "--stops", "bus", "--send")
        self.assertEqual(code, 0, err)
        st, _, body = self.call("GET", "/min.json")
        self.assertEqual(json.loads(body)["team"], {"version": "1.9.0", "reason": {"en": "the bus changed", "ru": "x"},
                                                    "since": "2026-11-12", "stops": ["bus"]})

    def test_a_script_is_stored_under_its_version_and_made_current_by_releases_json(self):
        sh = os.path.join(self.dir, "install.sh")
        with open(sh, "wb") as f:
            f.write(SH)
        code, out, err = self.up("script", sh, "--version", "1.0.0")
        self.assertEqual((code, self.releases()), (0, None), err)             # a look: nothing sent
        self.assertEqual(self.up("script", sh, "--version", "1.0.0", "--send")[0], 0)
        ref = {"url": "/v1/script/install-1.0.0.sh", "sha256": sha(SH), "size": len(SH), "version": "1.0.0"}
        self.assertEqual(self.releases()["scripts"], {"install.sh": ref})
        self.assertIn(b"HUB_DELIVER_KEY=" + READ.encode(), self.call("GET", f"/i/{READ}/install.sh")[2])
        self.assertEqual(self.up("script", sh, "--version", "1.0.0", "--send")[0], 0)      # the same again
        # a newer version of the same script; the other script keeps its own place
        with open(sh, "wb") as f:
            f.write(SH + b"echo new\n")
        ps = os.path.join(self.dir, "install.ps1")
        with open(ps, "wb") as f:
            f.write(PS)
        self.assertEqual(self.up("script", sh, "--version", "1.1.0", "--send")[0], 0)
        self.assertEqual(self.up("script", ps, "--version", "1.1.0", "--send")[0], 0)
        got = self.releases()["scripts"]
        self.assertEqual((got["install.sh"]["version"], got["install.ps1"]["url"]),
                         ("1.1.0", "/v1/script/install-1.1.0.ps1"))
        self.assertEqual(self.call("GET", "/v1/script/install-1.0.0.sh", key=READ)[2], SH)
        # other bytes under a version already published: refused, nothing moves
        with open(sh, "wb") as f:
            f.write(b"echo changed\n")
        self.assertEqual(self.up("script", sh, "--version", "1.1.0", "--send")[0], 3)
        self.assertEqual(self.releases()["scripts"]["install.sh"]["sha256"], sha(SH + b"echo new\n"))

    def test_the_server_a_script_is_put_on_is_written_in_where_it_marks_the_place(self):
        # curl <server>/i/<key>/install.sh | bash cannot tell where the script came from; the core names no server
        sh = os.path.join(self.dir, "install.sh")
        with open(sh, "wb") as f:
            f.write(b"#!/bin/bash\nS='@@HUB_DELIVER_URL@@'\nK='@@HUB_DELIVER_KEY@@'\nmain() { :; }\nmain \"$@\"\n")
        self.assertEqual(self.up("script", sh, "--version", "1.0.0", "--send")[0], 0)
        want = f"#!/bin/bash\nS='{self.url}'\nK='@@HUB_DELIVER_KEY@@'\nmain() {{ :; }}\nmain \"$@\"\n".encode()
        ref = self.releases()["scripts"]["install.sh"]
        self.assertEqual((ref["sha256"], ref["size"]), (sha(want), len(want)))       # releases.json names what is stored
        self.assertEqual(self.call("GET", f"/i/{READ}/install.sh")[2], want.replace(b"@@HUB_DELIVER_KEY@@", READ.encode()))
        with open(sh, "rb") as f:
            self.assertIn(b"@@HUB_DELIVER_URL@@", f.read())                         # the file itself is left as it was
        # sent through a tunnel: the public address is given; an address no installer can use is refused
        code, out, _ = self.up("script", sh, "--version", "1.1.0", "--address", "https://deliver.example.test/")
        self.assertEqual(code, 0)
        self.assertIn("the script names the server https://deliver.example.test", out)
        self.assertEqual(self.up("script", sh, "--version", "1.1.0", "--address", "http://203.0.113.9")[0], 2)
        self.assertEqual(self.up("script", sh, "--version", "1.1.0", "--address", "https://x.test/path")[0], 2)
        # the size the server takes is of the bytes sent, the address written in
        with open(sh, "wb") as f:
            f.write(b"@@HUB_DELIVER_URL@@" + b"#" * (256 * 1024 - len(b"@@HUB_DELIVER_URL@@")))
        self.assertEqual(self.up("script", sh, "--version", "1.2.0", "--send")[0], 2)

    def test_a_script_is_refused_before_sending_when_the_server_would_refuse_it(self):
        for name, data in (("install.sh", "café\n".encode()), ("setup.sh", SH), ("install.ps1", b"")):
            path = os.path.join(self.dir, name)
            with open(path, "wb") as f:
                f.write(data)
            self.assertEqual(self.up("script", path, "--version", "1.0.0", "--send")[0], 2, name)
        self.assertEqual(self.call("GET", "/v1/releases.json", key=READ)[0], 404)
        sh = os.path.join(self.dir, "install.sh")
        with open(sh, "wb") as f:
            f.write(SH)
        self.assertEqual(self.up("script", sh, "--version", "1 0", "--send")[0], 2)

    def test_an_environment_is_stored_as_an_app_file_and_named_for_its_platform(self):
        send = sys.modules["deliver_send"]
        self.assertEqual(send.SHELL_PLATFORMS, ("mac-arm64", "mac-x64", "linux-x64", "linux-arm64", "windows-x64",
                                                "windows-arm64"))
        env = os.path.join(self.dir, "agents-hub-v1.9.0-mac-x64.tar.gz")
        with open(env, "wb") as f:
            f.write(self.bytes)
        code, out, err = self.up("shell", env, "--platform", "mac-x64", "--runtime", "1")
        self.assertEqual((code, self.releases()), (0, None), err)
        self.assertEqual(self.up("shell", env, "--platform", "mac-x64", "--runtime", "1", "--send")[0], 0)
        self.assertEqual(self.call("GET", "/v1/app/agents-hub-v1.9.0-mac-x64.tar.gz", key=READ)[2], self.bytes)
        ref = {"url": "/v1/app/agents-hub-v1.9.0-mac-x64.tar.gz", "sha256": sha(self.bytes), "size": len(self.bytes),
               "runtime": 1}
        self.assertEqual(self.releases()["runtime"]["shells"], {"mac-x64": ref})
        # a second platform and a newer environment: each in its place; "current" is the apps' number, not touched
        win = os.path.join(self.dir, "agents-hub-v1.9.0-windows-arm64.tar.gz")
        with open(win, "wb") as f:
            f.write(b"win")
        self.assertEqual(self.up("shell", win, "--platform", "windows-arm64", "--runtime", "2", "--send")[0], 0)
        rt = self.releases()["runtime"]
        self.assertEqual((sorted(rt["shells"]), rt["current"]), (["mac-x64", "windows-arm64"], 0))
        # the layer's channel and the apps stay as they are
        self.assertEqual(self.up(*self.layer_args(), "--send")[0], 0)
        self.assertEqual(sorted(self.releases()["runtime"]["shells"]), ["mac-x64", "windows-arm64"])
        # wrong ask: a platform of the list, a name that ends with it, a number from 1, other bytes under the name
        for args in (("--platform", "mac-ppc", "--runtime", "1"), ("--platform", "linux-x64", "--runtime", "1"),
                     ("--platform", "mac-x64", "--runtime", "0")):
            self.assertEqual(self.up("shell", env, *args, "--send")[0], 2, args)
        with open(env, "wb") as f:
            f.write(b"changed")
        self.assertEqual(self.up("shell", env, "--platform", "mac-x64", "--runtime", "1", "--send")[0], 3)

    def test_the_bigger_environments_go_in_parts_as_the_apps_do(self):
        big = os.path.join(self.dir, "agents-hub-v1.9.0-linux-arm64.tar.gz")
        data = os.urandom(64 * 3 + 10)         # CHUNK is 64 in this test
        with open(big, "wb") as f:
            f.write(data)
        self.assertEqual(self.up("shell", big, "--platform", "linux-arm64", "--runtime", "1", "--send")[0], 0)
        self.assertEqual(self.call("GET", "/v1/app/agents-hub-v1.9.0-linux-arm64.tar.gz", key=READ)[2], data)



@cut.needs_deliver()
class ReleaseKeyStaysHome(unittest.TestCase):
    """Release review 2026-11-09: urllib follows a 30x of a GET and copies Authorization to any host — the release key
    (it publishes a layer to every hub) went to whatever a proxy or a redirect rule in front of the server named."""

    def test_a_redirect_is_not_followed_with_the_key(self):
        import http.server
        seen = []

        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)

        class Front(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{other.server_address[1]}/stolen")
                self.end_headers()

            def log_message(self, *a):
                pass
        front = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Front)
        for srv in (other, front):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
        send = load("deliver_send_redirect", SEND)
        status, _, _ = send.Client(f"http://127.0.0.1:{front.server_address[1]}", WRITE).call("GET", "/v1/releases.json")
        self.assertEqual(seen, [])
        self.assertEqual(status, 302)


if __name__ == "__main__":
    unittest.main()
