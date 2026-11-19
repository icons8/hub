"""What a release of the open build puts on its pages and among its GitHub release's files (tools/package/pages.py):
the core's own scripts with the pages' address and the open flavor written in, releases.json the scripts and the update
read (the layer on the pages, each environment at its absolute address among the release's files), the layer, min.json;
the environments apart for the release. A factory's out is laid out by hand here (its own run is in
tests/test_package_build.py); the scripts and the update reading these files run in tests/test_oneline.py and
tests/test_app_update.py."""
import tests  # noqa: F401 - keeps the machine's settings out
import base64
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest

import platforms

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SH = os.path.join(ROOT, "deploy", "oneline", "install.sh")
PS1 = os.path.join(ROOT, "deploy", "oneline", "install.ps1")
RELAY_SH = os.path.join(ROOT, "relay", "install.sh")
BASE = "https://acme.example.test/agents-hub"
ASSETS = "https://github.com/acme/agents-hub/releases/download/v1.2.3"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def raw(path):
    with open(path, "rb") as f:
        return f.read().replace(b"\r\n", b"\n")


def sha(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def sheet_of(path):
    return {"name": os.path.basename(path), "sha256": sha(path), "size": os.path.getsize(path)}


class Write(unittest.TestCase):
    def setUp(self):
        self.pages = load("hub_test_pages", os.path.join(ROOT, "tools", "package", "pages.py"))
        self.dir = tempfile.mkdtemp(prefix="hub-pages-")
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.rel = os.path.join(self.dir, "factory-out")
        self.out = os.path.join(self.dir, "publish")
        os.makedirs(self.rel)
        self.release()

    def release(self, flavor="open", platforms_=("linux-x64", "windows-x64"), runtime=3, runtime_min=3):
        """A factory's out of the tag v1.2.3: the layer (with the core's scripts in it), an environment archive a
        platform, release.json."""
        layer = os.path.join(self.rel, f"code-1.2.3-{flavor}.tar.gz")
        with tarfile.open(layer, "w:gz") as t:
            for name, path in (("deploy/oneline/install.sh", SH), ("deploy/oneline/install.ps1", PS1),
                               ("relay/install.sh", RELAY_SH)):
                data = raw(path)
                ti = tarfile.TarInfo(f"1.2.3-{flavor}/{name}")
                ti.size = len(data)
                t.addfile(ti, io.BytesIO(data))
        plats = {}
        for plat in platforms_:
            path = os.path.join(self.rel, f"agents-hub-v1.2.3-{'open-' if flavor == 'open' else ''}{plat}.tar.gz")
            with open(path, "wb") as f:
                f.write(f"the environment of {plat}\n".encode())
            plats[plat] = {"archive": sheet_of(path)}
        self.summary = {"tag": "v1.2.3", "runtime": runtime, "flavor": flavor,
                        "layer": {"name": f"1.2.3-{flavor}", "version": "1.2.3", "flavor": flavor, "channel": "stable",
                                  "commit": "c0ffee" * 6 + "abcd", "runtime_min": runtime_min, "archive": sheet_of(layer)},
                        "platforms": plats}
        self.save()

    def save(self):
        with open(os.path.join(self.rel, "release.json"), "w") as f:
            json.dump(self.summary, f)

    def write(self, **kw):
        return self.pages.write(self.rel, self.out, BASE, ASSETS, now=1_800_000_000, **kw)

    def read(self, *rel):
        with open(os.path.join(self.out, "pages", *rel), "rb") as f:
            return f.read()

    def test_the_pages_and_the_release_s_files(self):
        got = self.write(notes="- a new thing")
        self.assertEqual(got["pages"], [".nojekyll", "install.ps1", "install.sh", "min.json", "relay.sh",
                                        "v1/layer/code-1.2.3-open.tar.gz", "v1/releases.json"])
        relay = self.read("relay.sh")   # one's own relay: Docker builds this release's relay/ from its repository
        self.assertIn(b'REPO="https://github.com/acme/agents-hub.git"', relay)
        self.assertIn(b'TAG="v1.2.3"', relay)
        self.assertNotIn(b"@@HUB_RELAY_", relay)
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "release"))),
                         ["agents-hub-v1.2.3-open-linux-x64.tar.gz", "agents-hub-v1.2.3-open-windows-x64.tar.gz"])
        for name, p in self.summary["platforms"].items():
            self.assertEqual(sha(os.path.join(self.out, "release", p["archive"]["name"])), p["archive"]["sha256"])
        self.assertEqual(sha(os.path.join(self.out, "pages", "v1", "layer", "code-1.2.3-open.tar.gz")),
                         self.summary["layer"]["archive"]["sha256"])
        doc = json.loads(self.read("v1", "releases.json"))
        self.assertEqual(set(doc["channels"]), {"stable"})   # one channel (packaged hubs read stable only)
        linux = doc["runtime"]["shells"]["linux-x64"]
        self.assertEqual(linux, {"url": f"{ASSETS}/agents-hub-v1.2.3-open-linux-x64.tar.gz", "runtime": 3,
                                 **{k: self.summary["platforms"]["linux-x64"]["archive"][k] for k in ("sha256", "size")}})
        self.assertEqual(doc["runtime"]["current"], 3)
        self.assertEqual(json.loads(self.read("min.json")), {})
        # the update reads it as the team's (modules/update/layer.py entry_of)
        from modules.update import layer
        entry = layer.entry_of(doc, "stable")
        self.assertEqual((entry["version"], entry["runtime_min"], entry["notes"], entry["layer"]["url"]),
                         ("1.2.3", 3, ["a new thing"], "/v1/layer/code-1.2.3-open.tar.gz"))
        self.assertEqual(doc["channels"]["stable"]["published_at"], "2027-01-15T08:00:00Z")

    def test_the_scripts_are_the_core_s_with_the_address_and_the_open_flavor(self):
        self.write()
        sh, ps = self.read("install.sh"), self.read("install.ps1")
        self.assertEqual(sh, raw(SH).replace(b"@@HUB_DELIVER_URL@@", BASE.encode())
                         .replace(b"\nHUB_INSTALL_FLAVOR=team\n", b"\nHUB_INSTALL_FLAVOR=open\n"))
        self.assertIn(f"\nHUB_INSTALL_SERVER='{BASE}'".encode(), sh)
        self.assertIn(b"\nHUB_INSTALL_FLAVOR=open\n", sh)
        self.assertIn(f"\n  $Server0 = '{BASE}'".encode(), ps)
        self.assertIn(b"\n  $Flavor = 'open'\n", ps)
        for data in (sh, ps):
            self.assertNotIn(b"@@HUB_DELIVER_URL@@", data)
            self.assertTrue(self.pages.SCRIPT_TEXT.fullmatch(data))

    def test_the_core_s_scripts_have_each_flavor_line_once(self):
        # the pages' writer replaces exactly these lines: a script without them is one it cannot hand out
        for name, path in (("install.sh", SH), ("install.ps1", PS1)):
            old, _ = self.pages.FLAVOR_LINE[name]
            self.assertEqual(raw(path).count(old), 1, name)
            with self.assertRaises(self.pages.Refused):
                self.pages.open_script(name, raw(path) + old.lstrip(b"\n"), BASE)

    def test_install_sh_reads_the_environment_s_entry_of_these_pages(self):
        self.write()
        if os.name == "nt":
            self.skipTest("install.sh is for macOS and Linux")
        lines = raw(SH).decode().rstrip("\n").split("\n")[:-1]
        probe = os.path.join(self.dir, "probe.sh")
        with open(probe, "w") as f:
            f.write("\n".join(lines) + '\nfield "$(shell_entry "$1" linux-x64)" url\n')
        r = subprocess.run(["bash", probe, os.path.join(self.out, "pages", "v1", "releases.json")],  # portable: bash
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), f"{ASSETS}/agents-hub-v1.2.3-open-linux-x64.tar.gz", r.stderr)

    def test_a_release_not_of_the_open_build_or_not_as_its_sheet_says_is_refused(self):
        with self.assertRaisesRegex(self.pages.Refused, "base"):
            self.pages.write(self.rel, self.out, "http://203.0.113.5/agents-hub", ASSETS)
        with self.assertRaisesRegex(self.pages.Refused, "assets"):
            self.pages.write(self.rel, self.out, BASE, "https://github.com/acme/x?y=1")
        arc = os.path.join(self.rel, self.summary["platforms"]["linux-x64"]["archive"]["name"])
        with open(arc, "ab") as f:
            f.write(b"x")
        with self.assertRaisesRegex(self.pages.Refused, "linux-x64: .*size or sha256"):
            self.write()
        self.assertFalse(os.path.exists(self.out))   # nothing written
        self.release(runtime=2, runtime_min=3)
        with self.assertRaisesRegex(self.pages.Refused, "below the layer"):
            self.write()
        self.release(runtime_min=None)
        with self.assertRaisesRegex(self.pages.Refused, "runtime"):
            self.write()
        self.release()
        del self.summary["platforms"]["linux-x64"]["archive"]["size"]
        self.save()
        with self.assertRaisesRegex(self.pages.Refused, "linux-x64"):
            self.write()
        for name in os.listdir(self.rel):
            os.remove(os.path.join(self.rel, name))
        self.release(flavor="team")
        with self.assertRaisesRegex(self.pages.Refused, "team flavor"):
            self.write()

    def test_the_minimum_is_the_open_build_s_only(self):
        good = os.path.join(self.dir, "min.json")
        with open(good, "w") as f:
            json.dump({"open": {"version": "1.2.0", "reason": {"en": "x"}, "stops": []}}, f)
        self.write(minimum=good)
        self.assertEqual(json.loads(self.read("min.json"))["open"]["version"], "1.2.0")
        with open(good, "w") as f:
            json.dump({"team": {"version": "1.2.0"}}, f)
        with self.assertRaisesRegex(self.pages.Refused, "--min"):
            self.write(minimum=good)

    def test_files_not_on_a_github_release_leave_out_relay_sh_alone(self):
        got = self.pages.write(self.rel, self.out, BASE, "http://127.0.0.1:8/files", now=1_800_000_000)
        self.assertNotIn("relay.sh", got["pages"])   # a stand: no repository to build from; the rest is written
        self.assertIn("install.sh", got["pages"])

    def test_the_command_line(self):
        relay = os.path.join(self.dir, "relay.txt")
        payload = json.dumps({"v": 1, "relay": "https://relay.example.test", "key": "A" * 43 + "=", "id": "a" * 32,
                              "by": "operator", "exp": 4_000_000_000}, separators=(",", ":")).encode()
        sig = base64.urlsafe_b64encode(b"-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n")
        line = "hub-relay-invite-1:" + base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + \
            sig.decode().rstrip("=")
        with open(relay, "w") as f:
            f.write(line + "\n")
        for named, kept in ((relay, True), ("none", False)):
            with open(os.devnull, "w") as quiet:
                r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "package", "pages.py"), self.rel,
                                    "--out", self.out, "--base", BASE, "--assets", ASSETS, "--relay", named],
                                   capture_output=True, text=True, timeout=60, stdin=quiet)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("agents-hub-v1.2.3-open-linux-x64.tar.gz", r.stdout)
            self.assertTrue(os.path.isfile(os.path.join(self.out, "pages", "v1", "releases.json")))
            got = os.path.join(self.out, "pages", "v1", "relay.json")
            self.assertEqual(os.path.isfile(got), kept, named)
            if kept:
                self.assertEqual(json.loads(self.read("v1/relay.json")), {"invite": line})


if __name__ == "__main__":
    unittest.main()
