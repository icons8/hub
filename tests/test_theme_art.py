"""Theme art (owner request 03.10: a theme can take art): a theme names a picture next to itself —
"art": {"image": "city.jpg", …}; the server gives it the address /api/themes/art and serves only images from the
theme's folder (a plugin's or the user's own themes), no larger than 8 MB."""
import tests  # noqa: F401 — keep the machine's settings out
import json
import os
import tempfile
import unittest
from unittest import mock

from tests import test_web

JPEG = b"\xff\xd8\xff\xe0" + b"\0" * 64


class ThemeArt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-art-")
        self.plugin = os.path.join(self.tmp, "plugin")
        own = os.path.join(self.tmp, "cfg", "themes")
        os.makedirs(os.path.join(self.plugin, "art"))
        os.makedirs(own)
        theme = lambda name, image: {"name": name, "dark": True, "colors": {  # noqa: E731
            "background": "#0b0614", "foreground": "#e8f7ff", "primary": "#ff2fb4"},
            "art": {"image": image, "veil": 0.7, "glass": True}}
        with open(os.path.join(self.plugin, "neon.json"), "w") as f:
            json.dump(theme("Neon", "art/city.jpg"), f)
        with open(os.path.join(self.plugin, "escape.json"), "w") as f:
            json.dump(theme("Escape", "../outside.jpg"), f)
        with open(os.path.join(self.plugin, "art", "city.jpg"), "wb") as f:
            f.write(JPEG)
        with open(os.path.join(self.tmp, "outside.jpg"), "wb") as f:
            f.write(JPEG)
        with open(os.path.join(own, "mine.json"), "w") as f:
            json.dump(theme("Mine", "mine.webp"), f)
        with open(os.path.join(own, "mine.webp"), "wb") as f:
            f.write(b"RIFF\0\0\0\0WEBP")
        cfg = os.path.join(self.tmp, "cfg", "hub.json")
        with open(cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "update": {"enabled": False}, "extensions": {
                "alice:neon": {"path": self.plugin, "themes": ["neon.json", "escape.json"], "source": "alice/x",
                               "hub": "^1.0"}}}, f)
        self.env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        self.env.start()
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()

    def tearDown(self):
        self.h.tearDown()
        self.env.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def themes(self):
        return {t["theme"]["name"]: t for t in json.loads(self.h.req("GET", "/api/themes")[1])["themes"]}

    def test_the_answer_names_the_folder_of_this_hubs_own_themes(self):
        # a second hub's settings folder (HUB_CONFIG here, HUB_CONFIG_DIR of an instance): the hint of the page names it,
        # not the main hub's ~/.config/hub-core/themes/
        folder = json.loads(self.h.req("GET", "/api/themes")[1])["folder"]
        self.assertTrue(folder.replace("\\", "/").endswith("cfg/themes/"), folder)
        if os.name != "nt":
            with mock.patch.dict(os.environ, {"HOME": self.tmp}):
                self.assertEqual(json.loads(self.h.req("GET", "/api/themes")[1])["folder"], "~/cfg/themes/")

    def test_art_gets_an_address_and_is_served(self):
        got = self.themes()
        for name, kind in (("Neon", "image/jpeg"), ("Mine", "image/webp")):
            url = got[name]["theme"]["art"]["url"]
            self.assertTrue(url.startswith("/api/themes/art?id="), url)
            code, body = self.h.req("GET", url)
            self.assertEqual(code, 200, name)
        self.assertEqual(self.h.req("GET", got["Neon"]["theme"]["art"]["url"])[1], JPEG)

    def test_art_outside_the_theme_folder_is_refused(self):
        got = self.themes()
        self.assertNotIn("url", got["Escape"]["theme"]["art"])
        self.assertIn("art", got["Escape"].get("warning", ""))
        code, _ = self.h.req("GET", "/api/themes/art?id=alice:neon:escape.json")
        self.assertEqual(code, 404)
        code, _ = self.h.req("GET", "/api/themes/art?id=nobody:x.json")
        self.assertEqual(code, 404)

    def test_only_images_and_not_too_big(self):
        with open(os.path.join(self.plugin, "art", "city.jpg"), "wb") as f:
            f.truncate(9 * 1024 * 1024)
        code, _ = self.h.req("GET", "/api/themes/art?id=alice:neon:neon.json")
        self.assertEqual(code, 404)
        with open(os.path.join(self.plugin, "neon.json"), "w") as f:
            json.dump({"name": "Neon", "colors": {"background": "#000", "foreground": "#fff", "primary": "#f0f"},
                       "art": {"image": "neon.json"}}, f)
        self.assertNotIn("url", self.themes()["Neon"]["theme"]["art"])


class ThemeKit(unittest.TestCase):
    """A theme replaces icons, session emoji avatars and the heading font (owner request 03.10: extend the API so the
    profile icon, the background and the emoji set can be swapped for other icons). Files are only those declared in
    the theme's JSON and lying in its folder: images (png, jpg, webp, svg — as an image) and fonts (woff2, woff, ttf,
    otf)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hub-kit-")
        own = os.path.join(self.tmp, "cfg", "themes")
        os.makedirs(os.path.join(own, "kit"))
        for f, data in (("kit/account.png", b"\x89PNG"), ("kit/mushroom.svg", b"<svg/>"), ("kit/pixel.woff2", b"wOF2"),
                        ("kit/evil.js", b"alert(1)"), ("kit/secret.png", b"\x89PNG")):
            with open(os.path.join(own, f), "wb") as fh:
                fh.write(data)
        with open(os.path.join(own, "kit.json"), "w") as f:
            json.dump({"name": "Kit", "dark": False, "colors": {"background": "#5c94fc", "foreground": "#000", "primary": "#e52521"},
                       "icons": {"account": "kit/account.png", "clock": "kit/evil.js", "flow": "../outside.png"},
                       "avatars": {"🛠️": "kit/mushroom.svg"},
                       "font": "kit/pixel.woff2"}, f)
        cfg = os.path.join(self.tmp, "cfg", "hub.json")
        with open(cfg, "w") as f:
            json.dump({"dev": {"enabled": False}, "update": {"enabled": False}}, f)
        self.env = mock.patch.dict(os.environ, {"HUB_CONFIG": cfg})
        self.env.start()
        self.h = test_web.WebTest("test_page_and_snapshot")
        self.h.setUp()

    def tearDown(self):
        self.h.tearDown()
        self.env.stop()

    def test_icons_avatars_font_get_addresses_and_only_declared_files_are_served(self):
        (t,) = json.loads(self.h.req("GET", "/api/themes")[1])["themes"]
        th = t["theme"]
        self.assertEqual(sorted(th["icons_url"]), ["account"])                  # js and a path out of the folder — no
        self.assertIn("icons", t.get("warning", ""))
        self.assertEqual(list(th["avatars_url"]), ["🛠️"])
        for url in (th["icons_url"]["account"], th["avatars_url"]["🛠️"], th["font_url"]):
            self.assertEqual(self.h.req("GET", url)[0], 200, url)
        code, _ = self.h.req("GET", "/api/themes/file?id=own:kit.json&f=kit/secret.png")
        self.assertEqual(code, 404)                                              # not declared in the theme — not served
        code, _ = self.h.req("GET", "/api/themes/file?id=own:kit.json&f=kit/evil.js")
        self.assertEqual(code, 404)

    def test_the_list_reads_the_sources_once(self):
        """18.10: themes with hundreds of icons — /api/themes took ~1 s on every page open and in Files: each theme
        file re-collected all theme sources (and re-read hub.json); the list now collects them once per request."""
        from modules.web.server import Web
        calls = []
        real = Web.theme_sources
        with mock.patch.object(Web, "theme_sources", lambda w: calls.append(1) or real(w)):
            (t,) = json.loads(self.h.req("GET", "/api/themes")[1])["themes"]
        self.assertEqual(list(t["theme"]["icons_url"]), ["account"])
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
