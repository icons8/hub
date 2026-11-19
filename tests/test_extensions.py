"""Extensions outside the repo (owner decision 2026-10-02: an API and instructions for developing one's own vendor
plugins, for both the core and the interface): a process next to the core writes its own facts ext.<name>.<what>; the
page loads its module and serves files only from the extension's folder."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import shutil
import unittest

import platforms
from core import reducer
from tests import winsafe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE = os.path.join(ROOT, "examples", "extension-hello")


class Facts(unittest.TestCase):
    def test_extension_facts_are_recorded_and_change_nothing(self):
        st = reducer.empty()
        new, _, why = reducer.reduce(st, {"seq": 1, "ts": 1, "type": "ext.hello.stats", "entity": None,
                                          "data": {"turns_last_hour": {"a": 2}}})
        self.assertIsNone(why)
        self.assertEqual({k: v for k, v in new.items() if k != "seq"}, {k: v for k, v in st.items() if k != "seq"})
        self.assertIsNotNone(reducer.reduce(st, {"seq": 2, "ts": 1, "type": "ext.hello", "entity": None, "data": {}})[2])


class Web(unittest.TestCase):
    def setUp(self):
        from tests.test_web import WebTest
        self.helper = WebTest("test_page_and_snapshot")
        self.helper.setUp()
        cfg = os.environ["HUB_CONFIG"]
        with open(cfg) as f:
            self.saved = f.read()
        with open(cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "update": {"enabled": False},
                       "extensions": {"hello": {"path": EXAMPLE, "web": "web/main.js",
                                                "run": ["python3", "main.py"]},
                                      # from the marketplace — with an author (2026-10-03)
                                      "alice:hello": {"path": EXAMPLE, "web": "web/main.js", "source": "alice/x"}}}, f)

    def tearDown(self):
        with open(os.environ["HUB_CONFIG"], "w") as f:
            f.write(self.saved)
        self.helper.tearDown()

    def test_page_loads_the_extension_and_its_files_only(self):
        h = self.helper
        h.seed()
        snap = json.loads(h.req("GET", "/api/snapshot")[1])
        self.assertEqual(snap["extensions"], [{"name": "flow", "main": "main.js"},   # bundled Flow and Usage
                                              {"name": "usage", "main": "main.js"},
                                              {"name": "hello", "main": "web/main.js"},
                                              {"name": "alice:hello", "main": "web/main.js"}])
        self.assertEqual(h.req("GET", "/ext/alice:hello/web/main.js")[0], 200)
        code, js = h.req("GET", "/ext/hello/web/main.js")
        self.assertEqual(code, 200)
        self.assertIn(b"addView", js)
        for bad in ("/ext/hello/../../core/reducer.py", "/ext/hello/main.py", "/ext/nobody/web/main.js"):
            self.assertEqual(h.req("GET", bad)[0], 404, bad)

    def test_hub_up_runs_extension_processes(self):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("hub_up", os.path.join(ROOT, "bin", "hub-up"))
        spec = importlib.util.spec_from_loader("hub_up", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        # the command as the machine runs it (on Windows without a real python3 in PATH — the hub's own Python)
        cmd = platforms.current.ext_command(["python3", "main.py"])
        self.assertEqual(cmd[-1], "main.py")
        self.assertEqual(mod.extensions(), {"ext:hello": {"cmd": cmd, "cwd": EXAMPLE}})


class PluginMedia(unittest.TestCase):
    """A page plugin's own media (API 1.32): pictures, sound, video, wasm and 3D models from its folder, video and
    sound by Range, each with its own type and nosniff; nothing from outside the folder, a symlink out included."""

    TYPES = {"a.jpg": "image/jpeg", "a.jpeg": "image/jpeg", "a.webp": "image/webp", "a.gif": "image/gif",
             "a.mp4": "video/mp4", "a.webm": "video/webm", "a.mp3": "audio/mpeg", "a.ogg": "audio/ogg",
             "a.wav": "audio/wav", "a.wasm": "application/wasm", "a.glb": "model/gltf-binary"}

    def setUp(self):
        import tempfile
        from tests.test_web import WebTest
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "plug")
        os.makedirs(os.path.join(self.dir, "media"))
        with open(os.path.join(self.dir, "main.js"), "w") as f:
            f.write("export default function () {}\n")
        for name in self.TYPES:
            with open(os.path.join(self.dir, "media", name), "wb") as f:
                f.write(bytes(range(256)) * 4)   # 1024 bytes
        for name in ("a.exe", "my clip.mp4", "γάτα.jpg", "clip;v2.mp4", "MAIN.JS"):
            with open(os.path.join(self.dir, "media", name), "wb") as f:
                f.write(b"x" * 10)
        with open(os.path.join(self.tmp, "secret.mp4"), "wb") as f:
            f.write(b"outside")
        self.helper = WebTest("test_page_and_snapshot")
        self.helper.setUp()
        cfg = os.environ["HUB_CONFIG"]
        with open(cfg) as f:
            self.saved = f.read()
        with open(cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "update": {"enabled": False},
                       "extensions": {"plug": {"path": self.dir, "web": "main.js"}}}, f)

    def tearDown(self):
        with open(os.environ["HUB_CONFIG"], "w") as f:
            f.write(self.saved)
        self.helper.tearDown()
        from platforms import current
        current.rmtree(self.tmp)

    def get(self, path, headers=None):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.helper.port, timeout=5)
        c.request("GET", path, headers={"Host": f"127.0.0.1:{self.helper.port}", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, {k.lower(): v for k, v in r.getheaders()}, data

    def test_media_of_the_plugin_folder_is_served_with_its_type(self):
        self.helper.seed()
        for name, kind in self.TYPES.items():
            code, head, data = self.get(f"/ext/plug/media/{name}")
            self.assertEqual((code, head.get("content-type")), (200, kind), name)
            self.assertEqual(head.get("x-content-type-options"), "nosniff", name)
            self.assertEqual(len(data), 1024, name)
            self.assertNotIn("content-encoding", head, name)

    def test_video_and_sound_come_by_range(self):
        self.helper.seed()
        for name in ("a.mp4", "a.webm", "a.mp3", "a.ogg", "a.wav"):
            code, head, data = self.get(f"/ext/plug/media/{name}", {"Range": "bytes=10-19"})
            self.assertEqual(code, 206, name)
            self.assertEqual(head.get("content-range"), "bytes 10-19/1024", name)
            self.assertEqual(head.get("accept-ranges"), "bytes", name)
            self.assertEqual(data, bytes(range(10, 20)), name)
        self.assertEqual(self.get("/ext/plug/media/a.mp4", {"Range": "bytes=5000-"})[0], 416)
        code, head, data = self.get("/ext/plug/media/a.mp4", {"Range": "bytes=-"})   # not a range — the whole file
        self.assertEqual((code, len(data), head.get("content-range")), (200, 1024, None))

    def test_a_replaced_file_comes_whole_and_an_unchanged_one_is_not_sent_again(self):
        self.helper.seed()
        code, head, _ = self.get("/ext/plug/media/a.mp4")
        tag = head["etag"]
        self.assertFalse(tag.startswith("W/"), tag)   # browsers send If-Range only with a strong tag
        self.assertEqual(self.get("/ext/plug/media/a.mp4", {"If-None-Match": tag})[:1], (304,))
        self.assertEqual(self.get("/ext/plug/media/a.mp4", {"Range": "bytes=0-9", "If-Range": tag})[0], 206)
        with open(os.path.join(self.dir, "media", "a.mp4.new"), "wb") as f:
            f.write(b"new" * 400)
        # the core's replace: on Windows it waits while the server's last answer still holds the file open
        platforms.current.replace(os.path.join(self.dir, "media", "a.mp4.new"), os.path.join(self.dir, "media", "a.mp4"))
        code, _, data = self.get("/ext/plug/media/a.mp4", {"Range": "bytes=0-9", "If-Range": tag})
        self.assertEqual((code, data), (200, b"new" * 400))

    def test_names_with_spaces_and_letters_of_any_alphabet(self):
        self.helper.seed()
        from urllib.parse import quote
        for name, kind in (("my clip.mp4", "video/mp4"), ("γάτα.jpg", "image/jpeg"), ("clip;v2.mp4", "video/mp4")):
            code, head, data = self.get("/ext/plug/media/" + quote(name, safe=";"))
            self.assertEqual((code, head.get("content-type"), data), (200, kind, b"x" * 10), name)

    def test_nothing_from_outside_the_plugin_folder(self):
        self.helper.seed()
        link = os.path.join(self.dir, "media", "out.mp4")
        if not winsafe.try_symlink(os.path.join(self.tmp, "secret.mp4"), link):
            link = None   # Windows without the right to make links: the rest still holds
        for bad in ("/ext/plug/../secret.mp4", "/ext/plug/media/../../secret.mp4", "/ext/plug/media/%2e%2e/%2e%2e/secret.mp4",
                    "/ext/plug/media/a.exe", "/ext/plug/media/none.mp4", "/ext/nobody/media/a.mp4",
                    "/ext/plug/media/a.mp4%00.js", "/ext/plug/media/MAIN.JS"):
            self.assertEqual(self.get(bad)[0], 404, bad)
        if link:
            self.assertEqual(self.get("/ext/plug/media/out.mp4")[0], 404)
            self.assertEqual(self.get("/ext/plug/media/out.mp4", {"Range": "bytes=0-3"})[0], 404)


if __name__ == "__main__":
    unittest.main()
