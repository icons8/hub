"""The Embed API (core/embed.py, spec d2144426): where the core finds a company's code put into embed/<name>/, the rules an
embed keeps, and the names the core promises to call of each slot."""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import tests  # noqa: F401 — hide the machine's settings
import platforms
from core import cutout, embed, i18n
from tests import embed_contract
from tests.fakes import cut

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def write(top, rel, text=""):
    path = os.path.join(top, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def passport(top, name, api=None):
    write(top, f"embed/{name}/embed.json", json.dumps({"embed": 1, "api": api or embed.API}))


class Tree(unittest.TestCase):
    """A small tree of its own: the core's few files the cases need, and embeds in it."""

    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, self.top)
        write(self.top, "core/x.py")
        write(self.top, "modules/__init__.py")
        write(self.top, "modules/voice/__init__.py")
        write(self.top, "modules/voice/local.py")
        write(self.top, "tests/test_x.py")
        write(self.top, "i18n/en.json", json.dumps({"core.a": "A"}))


class Find(Tree):
    def test_an_embed_is_a_named_folder_with_a_passport(self):
        passport(self.top, "acme")
        write(self.top, "embed/notes/readme.txt")   # no passport — not an embed
        write(self.top, "embed/Bad Name/embed.json", "{}")
        self.assertEqual([n for n, _ in embed.roots(self.top)], ["acme"])

    def test_a_slot_is_present_in_the_core_or_in_an_embed_with_what_it_needs(self):
        self.assertFalse(embed.present("stats", self.top))
        passport(self.top, "acme")
        write(self.top, "embed/acme/modules/stats/stats.py")
        self.assertTrue(embed.present("stats", self.top))
        write(self.top, "embed/acme/modules/gateway/gateway.py")
        self.assertFalse(embed.present("gateway", self.top))   # the gateway needs the pool
        write(self.top, "modules/accounts/accounts.py")         # a shim of the move (SHIMS): no pool of the core's own
        self.assertFalse(embed.present("gateway", self.top))
        write(self.top, "embed/acme/modules/accounts/accounts.py")
        self.assertTrue(embed.present("gateway", self.top))
        self.assertEqual(embed.parts(["web", "gateway", "conduit", "stats"], self.top), ["web", "gateway", "stats"])

    def test_the_test_word_still_takes_a_slot_away(self):
        passport(self.top, "acme")
        write(self.top, "embed/acme/modules/stats/stats.py")
        os.environ["HUB_TEST_CUT"] = "stats"
        self.addCleanup(os.environ.pop, "HUB_TEST_CUT", None)
        self.assertFalse(embed.present("stats", self.top))

    def test_a_file_of_defaults_comes_from_the_core_first(self):
        passport(self.top, "acme")
        write(self.top, "embed/acme/modules/private.json", "{}")
        self.assertEqual(embed.file("modules/private.json", self.top),
                         os.path.join(self.top, "embed", "acme", "modules", "private.json"))
        write(self.top, "modules/private.json", "{}")
        self.assertEqual(embed.file("modules/private.json", self.top), os.path.join(self.top, "modules", "private.json"))

    def test_the_older_name_answers_the_same(self):
        self.assertEqual(cutout.FILES, {s: v["file"] for s, v in embed.SLOTS.items()})
        self.assertEqual(cutout.PARTS, {"gateway": "gateway", "conduit": "conduit", "stats": "stats"})
        passport(self.top, "acme")
        write(self.top, "embed/acme/modules/stats/stats.py")
        self.assertTrue(cutout.present("stats", self.top))
        self.assertEqual(cutout.parts(["stats", "conduit"], self.top), ["stats"])


class Packages(unittest.TestCase):
    """A copy of the core's import machinery in a folder of its own: the packages look into the embeds, a slot's folder
    that comes after the process started is found too, and hub-part runs a slot's part from an embed."""

    def setUp(self):
        self.top = tempfile.mkdtemp()
        self.addCleanup(platforms.current.rmtree, self.top)
        for rel in ("core/__init__.py", "core/embed.py", "core/version.py", "modules/__init__.py",
                    "modules/voice/__init__.py", "bin/hub-part"):
            os.makedirs(os.path.dirname(os.path.join(self.top, rel)), exist_ok=True)
            shutil.copy(os.path.join(ROOT, *rel.split("/")), os.path.join(self.top, *rel.split("/")))
        shutil.copytree(os.path.join(ROOT, "platforms"), os.path.join(self.top, "platforms"),
                        ignore=shutil.ignore_patterns("__pycache__"))
        passport(self.top, "acme")

    def py(self, code, *args):
        return subprocess.run([sys.executable, "-I", "-c", code, self.top, *args], capture_output=True, text=True,
                              timeout=60, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})

    def test_an_embeds_module_imports_by_the_cores_name(self):
        write(self.top, "embed/acme/modules/hello/__init__.py", "X = 'hello'\n")
        write(self.top, "embed/acme/modules/voice/speak.py", "Y = 'speak'\n")
        r = self.py("import sys; sys.path.insert(0, sys.argv[1]); from modules.hello import X;"
                    "from modules.voice.speak import Y; print(X, Y)")
        self.assertEqual(r.stdout.strip(), "hello speak", r.stderr)

    def test_a_slot_that_comes_after_the_start_is_found_without_a_restart(self):
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); import modules, modules.voice\n"
                "from core import embed\n"
                "print(embed.present('stats'))\n"
                "d = os.path.join(sys.argv[1], 'embed', 'acme', 'modules', 'stats'); os.makedirs(d)\n"
                "open(os.path.join(d, '__init__.py'), 'w').close()\n"
                "open(os.path.join(d, 'stats.py'), 'w').write('Z = 3')\n"
                "print(embed.present('stats'))\n"
                "from modules.stats import stats; print(stats.Z)\n")
        r = self.py(code)
        self.assertEqual(r.stdout.split(), ["False", "True", "3"], r.stderr)

    def test_a_whole_embed_that_comes_after_the_start_is_found_too(self):
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); import modules\n"
                "from core import embed\n"
                "d = os.path.join(sys.argv[1], 'embed', 'late', 'modules', 'stats'); os.makedirs(d)\n"
                "open(os.path.join(sys.argv[1], 'embed', 'late', 'embed.json'), 'w').write('{\"embed\": 1, \"api\": \"1.0\"}')\n"
                "open(os.path.join(d, '__init__.py'), 'w').close()\n"
                "open(os.path.join(d, 'stats.py'), 'w').write('Z = 4')\n"
                "print(embed.present('stats'))\n"
                "from modules.stats import stats; print(stats.Z)\n")
        r = self.py(code)
        self.assertEqual(r.stdout.split(), ["True", "4"], r.stderr)

    def test_a_second_embed_after_one_was_reached(self):
        write(self.top, "embed/acme/modules/stats/__init__.py")
        write(self.top, "embed/acme/modules/stats/stats.py", "Z = 1")
        code = ("import os, sys; sys.path.insert(0, sys.argv[1]); import modules, modules.voice\n"
                "from core import embed\n"
                "print(embed.present('stats'))\n"
                "d = os.path.join(sys.argv[1], 'embed', 'late', 'modules', 'voice'); os.makedirs(d)\n"
                "open(os.path.join(sys.argv[1], 'embed', 'late', 'embed.json'), 'w').write('{\"embed\": 1, \"api\": \"1.0\"}')\n"
                "open(os.path.join(d, 'speak.py'), 'w').write('W = 2')\n"
                "print(embed.present('speech'))\n"
                "from modules.voice import speak; print(speak.W)\n")
        r = self.py(code)
        self.assertEqual(r.stdout.split(), ["True", "True", "2"], r.stderr)

    def test_hub_part_runs_a_slots_main_from_an_embed(self):
        write(self.top, "embed/acme/modules/stats/__init__.py")
        write(self.top, "embed/acme/modules/stats/stats.py",
              "import sys\ndef main(argv, state_dir=None):\n    print('stats', *argv)\n    return 0\n")
        r = subprocess.run([sys.executable, "-I", os.path.join(self.top, "bin", "hub-part"), "stats"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "stats watch"), r.stderr)   # the empty line's default
        r = subprocess.run([sys.executable, "-I", os.path.join(self.top, "bin", "hub-part"), "stats", "--report"],
                           capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir())
        self.assertEqual(r.stdout.strip(), "stats --report", r.stderr)

    def test_hub_part_refuses_what_is_not_a_part_or_not_in_the_tree(self):
        write(self.top, "embed/acme/modules/accounts/__init__.py")
        write(self.top, "embed/acme/modules/accounts/accounts.py", "def main(args, st, put, me, cfg_path):\n    pass\n")
        for args in (["web"], ["gateway"], [], ["profile", "list"]):   # profile's main is `hub accounts`, not argv
            r = subprocess.run([sys.executable, "-I", os.path.join(self.top, "bin", "hub-part"), *args],
                               capture_output=True, text=True, timeout=60)
            self.assertNotEqual(r.returncode, 0, args)
            self.assertIn("hub-part:", r.stderr)


class Rules(Tree):
    def test_a_clean_embed_breaks_none(self):
        passport(self.top, "acme")
        write(self.top, "embed/acme/modules/stats/stats.py")
        write(self.top, "embed/acme/modules/voice/speak.py")
        write(self.top, "embed/acme/tests/test_stats.py")
        write(self.top, "embed/acme/i18n/en.json", json.dumps({"acme.b": "B"}))   # the core's file name: merged by key
        write(self.top, "embed/acme/profile.json", "{}")
        self.assertEqual(embed.problems(self.top, wanted=["acme"]), [])

    def test_each_rule(self):
        passport(self.top, "acme")
        passport(self.top, "beta")
        cases = {
            "embed/acme/core/x.py": "an embed carries only",
            "embed/acme/modules/voice/__init__.py": "the core's own file",
            "embed/acme/tests/test_x.py": "the core's own file",
            "embed/beta/modules/helper.py": "embed/acme/modules/helper.py too",
            "embed/beta/modules/stats/stats.py": "the slot stats is embed/acme's already",
        }
        write(self.top, "embed/acme/modules/helper.py")
        write(self.top, "embed/acme/modules/stats/stats.py")
        for rel in cases:
            write(self.top, rel)
        got = "\n".join(embed.problems(self.top))
        for rel, why in cases.items():
            self.assertTrue(any(rel in x and why in x for x in embed.problems(self.top)), (rel, why, got))

    def test_a_file_the_build_places_in_the_tree(self):
        # Embed API 1.2: an embed's deploy/ is laid at the same path of a team build's layer (the company's signed Mac
        # launcher): a file the core carries is never replaced, nor one another embed places
        passport(self.top, "acme")
        passport(self.top, "beta")
        write(self.top, "deploy/mac/install.sh")
        write(self.top, "embed/acme/deploy/mac/Acme.app/Contents/Info.plist")
        self.assertEqual(embed.problems(self.top), [])
        write(self.top, "embed/acme/deploy/mac/install.sh")
        write(self.top, "embed/beta/deploy/mac/Acme.app/Contents/Info.plist")
        got = embed.problems(self.top)
        self.assertTrue(any("embed/acme/deploy/mac/install.sh: the core's own file" in x for x in got), got)
        self.assertTrue(any("embed/beta/deploy/mac/Acme.app/Contents/Info.plist: embed/acme/deploy/mac/Acme.app/Contents/"
                            "Info.plist too" in x for x in got), got)
        # a bundle is whole (its signature seals every file): none laid into a bundle the core carries
        write(self.top, "deploy/mac/Core.app/Contents/Info.plist")
        write(self.top, "embed/acme/deploy/mac/Core.app/Contents/MacOS/Core")
        self.assertTrue(any("embed/acme/deploy/mac/Core.app/Contents/MacOS/Core: deploy/mac/Core.app is the core's" in x
                            for x in embed.problems(self.top)), embed.problems(self.top))
        os.unlink(os.path.join(self.top, "embed", "acme", "deploy", "mac", "Core.app", "Contents", "MacOS", "Core"))
        write(self.top, "embed/beta/deploy/mac/Acme.app/Contents/MacOS/Acme")   # another file of acme's bundle
        got = embed.problems(self.top)
        self.assertTrue(any(x.startswith("embed/beta/deploy/mac/Acme.app/") and "deploy/mac/Acme.app is embed/acme's" in x
                            for x in got), got)
        self.assertEqual(len([x for x in got if "Acme.app is embed/acme's" in x]), 1, got)   # one line a bundle
        os.unlink(os.path.join(self.top, "embed", "beta", "deploy", "mac", "Acme.app", "Contents", "MacOS", "Acme"))
        self.assertEqual(embed.placed(self.top), [("acme", "deploy/mac/Acme.app/Contents/Info.plist"),
                                                  ("acme", "deploy/mac/install.sh"),
                                                  ("beta", "deploy/mac/Acme.app/Contents/Info.plist")])

    def test_the_profile_names_exactly_the_embeds_of_the_tree(self):
        passport(self.top, "acme")
        passport(self.top, "beta")
        got = embed.problems(self.top, wanted=["acme", "gamma"])
        self.assertIn("embed/beta: not in the profile", got)
        self.assertIn("embed/gamma: the profile's embed is not in the tree", got)

    def test_a_folder_of_embed_that_is_no_embed(self):
        write(self.top, "embed/notes/readme.txt")
        self.assertTrue(any("embed/notes: not an embed" in x for x in embed.problems(self.top)))

    def test_the_api_number(self):
        major, minor = map(int, embed.API.split("."))
        for api, ok in ((f"{major}.{minor}", True), (f"{major}.0", True), (f"{major}.{minor + 1}", False),
                        (f"{major + 1}.0", False), ("one", False)):
            platforms.current.rmtree(os.path.join(self.top, "embed"))
            passport(self.top, "acme", api)
            self.assertEqual(embed.problems(self.top) == [], ok, (api, embed.problems(self.top)))

    def test_a_phrase_key_is_the_cores_or_one_embeds(self):
        passport(self.top, "acme")
        passport(self.top, "beta")
        write(self.top, "embed/acme/i18n/en.json", json.dumps({"core.a": "mine", "acme.b": "B"}))
        write(self.top, "embed/beta/i18n/en.json", json.dumps({"acme.b": "theirs"}))
        got = embed.problems(self.top)
        self.assertTrue(any("embed/acme/i18n/en.json: the key core.a is the core's" in x for x in got), got)
        self.assertTrue(any("embed/beta/i18n/en.json: the key acme.b is embed/acme's" in x for x in got), got)

    @unittest.skipIf(os.name == "nt", "a link needs a right on Windows")
    def test_a_link_is_refused(self):
        passport(self.top, "acme")
        os.makedirs(os.path.join(self.top, "embed", "acme", "modules"))
        os.symlink(os.path.join(self.top, "core", "x.py"), os.path.join(self.top, "embed", "acme", "modules", "x.py"))
        self.assertTrue(any("modules/x.py: a link" in x for x in embed.problems(self.top)))


class Modules(Tree):
    """The Module API modules an embed carries (embed/<name>/modules/<module>/module.json) are built-in ones, as the
    core's own: found without hub module add, on by their manifest."""

    def module(self, rel, name, **extra):
        write(self.top, f"{rel}/module.json", json.dumps({"name": name, "version": "1.0.0", "api": "^2.0",
                                                          "run": ["python3", "main.py"], **extra}))
        return os.path.join(self.top, *rel.split("/"))

    def resolve(self, cfg=None):
        from unittest import mock
        from core import modules
        with mock.patch.object(modules, "REPO", self.top):
            found = modules.discover(cfg or {}, self.top)
            return found, {(r["name"], r["dir"]): r for r in modules.resolve(found, cfg or {})}

    def test_an_embeds_module_is_built_in_and_on_by_its_manifest(self):
        passport(self.top, "acme")
        hello = self.module("embed/acme/modules/hello", "hello")
        quiet = self.module("embed/acme/modules/quiet", "quiet", default_off=True)
        found, got = self.resolve()
        self.assertEqual([(f["name"], f["origin"], f.get("embed")) for f in found],
                         [("hello", "builtin", "acme"), ("quiet", "builtin", "acme")])
        self.assertEqual(got[("hello", hello)]["state"], "ok")   # no modules.enabled, no hub module add
        self.assertEqual(got[("quiet", quiet)]["state"], "disabled")
        self.assertEqual(self.resolve({"modules": {"enabled": ["quiet"]}})[1][("quiet", quiet)]["state"], "ok")
        self.assertEqual(embed.problems(self.top), [])

    def test_a_folder_without_a_passport_brings_no_modules(self):
        self.module("embed/notes/modules/hello", "hello")
        self.assertEqual(self.resolve()[0], [])

    def test_the_cores_module_of_the_same_name_stays_and_the_embeds_is_refused(self):
        passport(self.top, "acme")
        own = self.module("modules/hello", "hello")
        copy = self.module("embed/acme/modules/greeter", "hello")
        user = self.module("embed/acme/modules/user", "user", requires={"modules": {"hello": "^1.0"}})
        got = self.resolve()[1]
        self.assertEqual(got[("hello", own)]["state"], "ok")
        self.assertEqual(got[("hello", copy)]["state"], "blocked")
        self.assertIn(copy, got[("hello", copy)]["reason"])
        self.assertEqual(got[("user", user)]["state"], "ok")   # the copy does not take hello away from whoever needs it
        self.assertIn("embed/acme/modules/greeter/module.json: the module hello is the core's",
                      "\n".join(embed.problems(self.top)))

    def test_a_core_module_with_a_broken_manifest_still_owns_its_folders_name(self):
        passport(self.top, "acme")
        write(self.top, "modules/hello/module.json", "{")   # the runtime names it by its folder (modules.read)
        self.module("embed/acme/modules/hi", "hello")
        self.assertIn("embed/acme/modules/hi/module.json: the module hello is the core's",
                      "\n".join(embed.problems(self.top)))

    def test_two_embeds_with_one_module_name(self):
        passport(self.top, "acme")
        passport(self.top, "beta")
        a = self.module("embed/acme/modules/hello", "hello")
        b = self.module("embed/beta/modules/hi", "hello")
        got = self.resolve()[1]
        self.assertEqual((got[("hello", a)]["state"], got[("hello", b)]["state"]), ("blocked", "blocked"))
        self.assertIn("embed/beta/modules/hi/module.json: the module hello is embed/acme's already",
                      "\n".join(embed.problems(self.top)))

    def test_the_folder_hub_module_add_named_before_is_the_built_in_module(self):
        passport(self.top, "acme")
        hello = self.module("embed/acme/modules/hello", "hello")
        tops = [self.top]
        if os.name != "nt":   # <state>/code is a link to the code's layer: the path through it is the same folder
            os.symlink(self.top, self.top + "-code")
            self.addCleanup(os.unlink, self.top + "-code")
            tops.append(self.top + "-code")
        for top in tops:
            cfg = {"modules": {"paths": [os.path.join(top, "embed", "acme", "modules")], "enabled": ["hello"]}}
            found, got = self.resolve(cfg)
            self.assertEqual([(f["name"], f["origin"]) for f in found], [("hello", "builtin")], top)
            self.assertEqual(got[("hello", hello)]["state"], "ok")
            from unittest import mock
            from core import modules
            with mock.patch.object(modules, "REPO", self.top):   # the update report lists the hub's own modules only
                self.assertEqual(modules.discover(cfg, self.top, builtin=False), [], top)

    def test_without_embeds_the_core_sees_its_own_modules_only(self):
        own = self.module("modules/hello", "hello")
        found, got = self.resolve()
        self.assertEqual([(f["name"], f["origin"], f.get("embed")) for f in found], [("hello", "builtin", None)])
        self.assertEqual(got[("hello", own)]["state"], "ok")

    def test_a_process_with_the_previous_core_embed_in_memory(self):
        # a process of the previous release keeps core.embed of 1.0 (no module_folders) and imports the new
        # core/modules.py from the disk: it sees no embed modules until its restart, and does not fall
        import types
        from unittest import mock
        import core
        passport(self.top, "acme")
        self.module("embed/acme/modules/hello", "hello")
        with mock.patch.object(core, "embed", types.SimpleNamespace(roots=embed.roots, API="1.0")):
            self.assertEqual(self.resolve()[0], [])

    def test_the_owners_switch_of_an_embeds_module(self):
        from core import modules
        passport(self.top, "acme")
        hello = self.module("embed/acme/modules/hello", "hello")
        quiet = self.module("embed/acme/modules/quiet", "quiet", default_off=True)
        kept = {"modules": {"enabled": ["other", 3]}}   # entries it does not understand stay as they are
        self.assertEqual(modules.switch(kept, "quiet", True, True), [("modules.enabled", ["other", 3], ["other", 3, "quiet"]),
                                                                     ("modules.off", [], [])])
        cfg = {"theme": "x", "modules": {"enabled": ["other"]}}
        self.assertEqual(modules.switch(cfg, "hello", False, True), [("modules.enabled", ["other"], ["other"]),
                                                                     ("modules.off", [], ["hello"])])
        self.assertEqual(cfg["modules"]["off"], ["hello"])
        got = self.resolve(cfg)[1][("hello", hello)]
        self.assertEqual((got["state"], got["reason"]), ("disabled", i18n.t("core.modules.turned_off")))
        modules.switch(cfg, "quiet", True, True)
        self.assertEqual(self.resolve(cfg)[1][("quiet", quiet)]["state"], "ok")
        modules.switch(cfg, "hello", True, True)
        self.assertEqual((cfg["modules"]["off"], cfg["modules"]["enabled"]), ([], ["other", "quiet", "hello"]))
        self.assertEqual(self.resolve(cfg)[1][("hello", hello)]["state"], "ok")
        modules.switch(cfg, "quiet", False, True)
        self.assertEqual(self.resolve(cfg)[1][("quiet", quiet)]["state"], "disabled")
        # a folder's module has no modules.off: only enabled
        plain = {"modules": {"enabled": ["a"]}}
        self.assertEqual(modules.switch(plain, "a", False, False), [("modules.enabled", ["a"], [])])
        self.assertEqual(plain, {"modules": {"enabled": []}})
        for bad in ("abc", -1, None, [3]):   # a typo in hub.json reads as nothing turned off
            self.assertEqual(self.resolve({"modules": {"off": bad}})[1][("hello", hello)]["state"], "ok", bad)

    def test_developer_mode_of_an_embeds_module_is_its_own_folder(self):
        from unittest import mock
        from core import modsrc, modules
        passport(self.top, "acme")
        hello = self.module("embed/acme/modules/hello", "hello")
        copy = self.module("mine/hello", "hello")
        cfg_path = os.path.join(self.top, "hub.json")
        with open(cfg_path, "w") as f:
            json.dump({"modules": {"paths": [os.path.dirname(copy)]}}, f)
        with mock.patch.object(modules, "REPO", self.top):
            modsrc.set_dev(self.top, cfg_path, "hello")
            with open(cfg_path) as f:
                cfg = json.load(f)
            self.assertEqual(cfg["modules"]["dev"], ["hello"])
            found = modules.discover(cfg, self.top)
            self.assertEqual(modules.dev_of(cfg, found), {"hello": hello})   # the copy in another folder does not take it
        got = self.resolve(cfg)[1]
        self.assertEqual((got[("hello", hello)]["state"], got[("hello", copy)]["state"]), ("ok", "blocked"))

    def test_the_build_refuses_a_manifest_the_hub_would_block(self):
        passport(self.top, "acme")
        self.module("embed/acme/modules/good", "good")
        write(self.top, "embed/acme/modules/broken/module.json", "{")
        self.module("embed/acme/modules/odd", "odd", api="<3", colour="red")
        self.module("embed/acme/modules/later", "later", api="^9.0")
        got = "\n".join(embed.problems(self.top))
        self.assertNotIn("modules/good/", got)
        self.assertIn("embed/acme/modules/broken/module.json: " + i18n.t("core.modules.json_unreadable", error="")[:20], got)
        self.assertIn("embed/acme/modules/odd/module.json: api:", got)
        self.assertIn("embed/acme/modules/odd/module.json: colour: unknown field", got)
        self.assertIn("embed/acme/modules/later/module.json: ", got)
        self.module("modules/web", "web-core")   # a folder of the core's modules/: modules.web is the core's package
        self.module("embed/acme/modules/web", "acme-web")
        self.assertIn("embed/acme/modules/web/module.json: the folder modules/web is the core's", "\n".join(embed.problems(self.top)))
        later = self.resolve()[1][("later", os.path.join(self.top, "embed", "acme", "modules", "later"))]
        self.assertEqual(later["state"], "blocked")   # what the build says is what the hub would say
        self.assertIn(later["reason"], got)

    def test_an_embeds_module_brings_its_catalog_tools(self):
        import importlib
        import sys as _sys
        from unittest import mock
        import modules as package
        from core import catalog
        passport(self.top, "acme")
        name = "embedtools" + os.path.basename(self.top).replace("-", "_").replace(".", "_")
        self.module(f"embed/acme/modules/{name}", "tools")
        write(self.top, f"embed/acme/modules/{name}/catalog.py",
              "def entries(cfg):\n    return {'acme:tool': {'kind': 'mcp', 'cfg': sorted(cfg)}}\n")

        def drop():
            for k in [k for k in _sys.modules if k.startswith(f"modules.{name}")]:
                _sys.modules.pop(k)
        self.addCleanup(drop)
        with mock.patch.object(catalog, "REPO", self.top), \
                mock.patch.object(package, "__path__", list(package.__path__) + [
                    os.path.join(self.top, "embed", "acme", "modules")]):
            importlib.invalidate_caches()
            self.assertEqual(catalog.module_tools({"a": 1}), {"acme:tool": {"kind": "mcp", "cfg": ["a"]}})
            # turned off on the page — its tools go with it
            self.assertEqual(catalog.module_tools({"modules": {"off": ["tools"]}}), {})


class PinOfARelease(Tree):
    def test_the_update_gate_reads_an_embeds_pin_of_the_target(self):
        from modules.update import private

        def g(*args):
            return subprocess.run(["git", *args], cwd=self.top, capture_output=True, text=True, timeout=60).stdout.strip()
        g("init", "-q")
        passport(self.top, "acme")
        write(self.top, "embed/acme/modules/private.json", '{"repo": "a/b"}')
        write(self.top, "embed/zz/modules/private.json", '{"repo": "no/passport"}')   # not an embed
        g("add", "-A")
        g("-c", "user.name=x", "-c", "user.email=x@example.test", "commit", "-qm", "c")
        self.assertEqual(private.pin_at(g, "HEAD"), '{"repo": "a/b"}')
        write(self.top, "modules/private.json", '{"repo": "core/own"}')
        g("add", "-A")
        g("-c", "user.name=x", "-c", "user.email=x@example.test", "commit", "-qm", "d")
        self.assertEqual(private.pin_at(g, "HEAD"), '{"repo": "core/own"}')   # the core's own first
        self.assertEqual(private.pin_at(g, "HEAD~1"), '{"repo": "a/b"}')


class Phrases(Tree):
    def test_an_embeds_catalog_joins_the_cores_and_the_core_keeps_its_own(self):
        passport(self.top, "acme")
        write(self.top, "embed/acme/i18n/en.json", json.dumps({"acme.b": "B", "core.a": "not this"}))
        old, cache = i18n.ROOT, dict(i18n._cache)
        i18n.ROOT = os.path.join(self.top, "i18n")
        i18n._cache.clear()

        def back():
            i18n.ROOT = old
            i18n._cache.clear()
            i18n._cache.update(cache)
        self.addCleanup(back)
        self.assertEqual(i18n.catalog("en"), {"core.a": "A", "acme.b": "B"})
        self.assertIs(i18n.catalog("en"), i18n.catalog("en"))   # merged once, not on every string
        path = os.path.join(self.top, "embed", "acme", "i18n", "en.json")
        os.utime(os.path.join(self.top, "i18n", "en.json"), ns=(10 ** 18, 10 ** 18))   # the core's: older than both
        os.utime(path, ns=(2 * 10 ** 18, 2 * 10 ** 18))
        i18n._cache.clear()
        before = i18n.stamp("en")
        os.utime(path, ns=(3 * 10 ** 18, 3 * 10 ** 18))
        i18n._cache.clear()
        self.assertNotEqual(i18n.stamp("en"), before)   # the browser's cached strings go with an embed's change too
        before = i18n.stamp("en")
        platforms.current.rmtree(os.path.join(self.top, "embed", "acme"))
        i18n._cache.clear()
        self.assertNotEqual(i18n.stamp("en"), before)   # ...and with one that leaves

    def test_the_check_of_an_embeds_catalogs_asks_its_languages_and_the_whole_ones(self):
        from tools import i18n as tool
        passport(self.top, "acme")
        write(self.top, "embed/acme/i18n/en.json", json.dumps({"acme.b": "B"}))
        write(self.top, "i18n/stage.json", json.dumps({"translated": ["en", "ru"], "ready": ["en", "ru", "de"]}))

        def langs(got):
            self.assertTrue(all(x.startswith("embed/acme: ") for x in got), got)
            return sorted({x.split(":")[1].strip() for x in got})
        self.assertEqual(langs(tool.embedded(self.top)), ["ru"])                  # the whole ones, not every language
        self.assertEqual(langs(tool.embedded(self.top, release=True)), ["de", "ru"])   # a release: the ready ones too


class Contract(unittest.TestCase):
    def test_signatures(self):
        self.assertTrue(embed.fits("a, b, c=", "a, b"))
        self.assertTrue(embed.fits("a, b=, *rest, **kw", "a, b="))
        self.assertTrue(embed.fits("a, b=", "a, b"))          # the core passes it, a default does not hurt
        self.assertFalse(embed.fits("a, b", "a, b="))         # the core may leave b out
        self.assertFalse(embed.fits("a, c", "a, b"))
        self.assertFalse(embed.fits("a", "a, b"))
        self.assertFalse(embed.fits("a, b, c", "a, b"))       # an extra required one

    def test_the_kinds_of_parameters(self):
        def kw_only(*, file=None):
            pass

        def pos_only(a, b, /):
            pass

        def star(*argv):
            pass

        def tail(argv, *, why=None, **more):
            pass
        self.assertFalse(embed.fits(embed.signature(kw_only), "file="))   # the core passes it by position
        self.assertFalse(embed.fits(embed.signature(pos_only), "a, b"))  # ...or by name
        self.assertFalse(embed.fits(embed.signature(star), "argv"))      # main(["watch"]) gets a tuple
        self.assertTrue(embed.fits(embed.signature(tail), "argv"))
        self.assertEqual(embed.signature(tail), "argv, *, why=, **more")

    def test_a_module_that_lacks_a_name_or_takes_it_otherwise(self):
        class Speech:
            class SpeakError(RuntimeError):
                pass

            @staticmethod
            def reply(text, cfg, voice=None, account=None):
                return None

            @staticmethod
            def settings(cfg, more):
                return {}
        got = embed.check("speech", Speech)
        self.assertIn("speech: no missing", got)
        self.assertTrue(any(x.startswith("speech: settings(cfg, more)") for x in got), got)
        self.assertFalse(any("reply" in x or "SpeakError" in x for x in got), got)

    def test_an_optional_name_of_a_later_minor_number(self):
        class Conduit:
            pass
        old = dict(embed.OPTIONAL)
        self.addCleanup(lambda: (embed.OPTIONAL.clear(), embed.OPTIONAL.update(old)))
        embed.OPTIONAL["conduit"] = {"main": "1.1"}
        self.assertEqual(embed.check("conduit", Conduit, api="1.0"), [])
        self.assertEqual(embed.check("conduit", Conduit, api="1.1"), ["conduit: no main"])

    def test_the_code_in_this_tree_fits_the_table(self):
        import importlib
        for slot, spec in embed.SLOTS.items():
            with self.subTest(slot=slot):
                if not embed.present(slot):
                    self.skipTest(f"the code of {slot} is not in this tree")
                self.assertEqual(embed.check(slot, importlib.import_module(spec["module"])), [])


class Calls(unittest.TestCase):
    """The core calls of a slot only what the table promises: a new call is a new name of the API."""

    def calls(self):
        modules = {spec["module"]: slot for slot, spec in embed.SLOTS.items()}
        skip = tuple(os.path.dirname(spec["file"]) + "/" for spec in embed.SLOTS.values()
                     if not spec["file"].startswith("modules/voice/")) + ("tests/", embed.FOLDER + "/")
        skip_files = {spec["file"] for spec in embed.SLOTS.values()}
        got = subprocess.run(["git", "ls-files", "*.py", "bin/*"], cwd=ROOT, capture_output=True, text=True, timeout=60)
        if got.returncode:
            self.skipTest("not a git checkout")
        out = {}
        for rel in got.stdout.split():
            if rel.startswith(skip) or rel in skip_files:
                continue
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                    tree = ast.parse(f.read())
            except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
                continue
            alias = {}
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.module:
                    for a in n.names:
                        if f"{n.module}.{a.name}" in modules:
                            alias[a.asname or a.name] = modules[f"{n.module}.{a.name}"]
                        elif n.module in modules:
                            out.setdefault(modules[n.module], set()).add((a.name, rel))
            for n in ast.walk(tree):
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in alias:
                    out.setdefault(alias[n.value.id], set()).add((n.attr, rel))
        return out

    def test_every_call_of_a_slot_is_in_the_table(self):
        extra = sorted(f"{slot}.{name} ({rel})" for slot, names in self.calls().items() for name, rel in names
                       if name not in embed.NAMES[slot])
        self.assertEqual(extra, [], "a new name the core calls of a slot: add it to core/embed.NAMES (and OPTIONAL "
                                    "with the next minor EMBED_API, core/version.py)")

    def test_the_parts_run_the_slots_main(self):
        for part, (slot, _) in embed.PARTS.items():
            self.assertIn("main", embed.NAMES[slot], part)


class ThisTree(unittest.TestCase):
    def test_this_tree_keeps_the_rules(self):
        self.assertEqual(embed.problems(ROOT), [])

    def test_a_phrase_only_an_embed_names_is_in_its_catalogs(self):
        """An embed's phrases go with it (the open flavor has none of them): a key only an embed's code names and the
        core's catalog carries is a leak of its words into the open core."""
        import re
        from tools import i18n as tool
        if not os.path.exists(os.path.join(ROOT, ".git")):
            self.skipTest("not a git checkout")
        core_en = i18n._read("en.json")
        built = re.compile(r"""["'`]([a-z0-9_]+(?:\.[a-z0-9_]+)+\.)(?:\{|["'`])""")
        theirs, ours, prefixes = set(), set(tool.DYNAMIC), set()
        for rel in tool.public_files(ROOT):   # what tools/i18n.py used_keys reads
            mine = embed.core_path(rel)
            if mine.startswith(("tests/", "evals/", "docs/", "i18n/", "tools/")) or \
                    not rel.endswith((".py", ".js", ".mjs", ".html")):
                continue
            with open(os.path.join(ROOT, rel), encoding="utf-8", errors="replace") as f:
                text = f.read()
            keys = set(tool.USES.findall(text))
            if mine != rel:
                theirs |= keys
            else:
                ours |= keys
                prefixes |= set(built.findall(text))
        leak = sorted(k for k in theirs - ours if k in core_en and not any(k.startswith(p) for p in prefixes))
        self.assertEqual(leak, [], "move them into embed/<name>/i18n/<lang>.json (core/embed.py)")

    def test_a_file_by_its_place_in_the_cores_layout(self):
        self.assertEqual(embed.core_path("embed/acme/modules/stats/stats.py"), "modules/stats/stats.py")
        self.assertEqual(embed.core_path("modules/stats/stats.py"), "modules/stats/stats.py")
        self.assertEqual(embed.core_path("embed/Bad Name/x.py"), "embed/Bad Name/x.py")


def _own(slot, mixin):
    """The contract of slot run on the core's own code of it (skipped where the tree has none)."""
    import importlib

    @cut.needs(slot)
    class Own(mixin, unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            cls.module = importlib.import_module(embed.SLOTS[slot]["module"])
    Own.__name__ = Own.__qualname__ = f"Own{slot.capitalize()}"
    return Own


# one class per slot, for unittest to find: the core's own code of it keeps the contract an embed's code keeps
globals().update({cls.__name__: cls for cls in (_own(slot, embed_contract.SLOTS[slot]) for slot in embed.SLOTS)})


class EverySlot(unittest.TestCase):
    def test_every_slot_has_its_contract(self):
        self.assertEqual(set(embed_contract.SLOTS), set(embed.SLOTS))
