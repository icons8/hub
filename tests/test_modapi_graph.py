"""Module API: the module.json manifest, module discovery and the dependency graph (core/modules.py, the Module API spec, slice 1).

A module is a folder with module.json and its own process (the run field). An external module loads only from modules.enabled;
if it cannot load, it is blocked with a reason a person can fix, and the block spreads to dependents."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import tempfile
import unittest

from core import modules, version


def manifest(name, **extra):
    return {"name": name, "version": "1.0.0", "api": "^2.0", "run": ["python3", "main.py"], **extra}


class Versions(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(version.parse_version("1.2"), (1, 2, 0))
        self.assertIsNone(version.parse_version("1.x"))
        self.assertTrue(version.satisfies("1.4.0", "^1.2"))
        self.assertFalse(version.satisfies("2.0.0", "^1.2"))
        self.assertTrue(version.satisfies("1.2.9", ">=1.2,<1.3"))
        self.assertTrue(version.satisfies("3.0.0", "*"))
        self.assertFalse(version.valid_range("~1.2"))
        self.assertFalse(version.has_floor("<2"))

    def test_module_api_is_the_main_line_two(self):
        self.assertEqual(version.MODULE_API.split(".")[0], "2")
        self.assertEqual(version.module_api("^2.0"), (True, ""))
        ok, why = version.module_api("^1.0")
        self.assertFalse(ok)   # a module of another line (modules inside the core process) — a clear refusal, not silent corruption
        self.assertIn("1.x", why)
        self.assertIn(version.MODULE_API, why)
        ok, why = version.module_api("^2.99")
        self.assertFalse(ok)
        self.assertIn("update the hub", why)

    def test_points_of_each_version_are_listed(self):
        self.assertIn("hello", version.module_points())
        self.assertEqual(version.module_points("2.0"), version.MODULE_POINTS["2.0"])

    def test_a_module_for_api_2_0_gets_nothing_of_2_1_and_later(self):
        # API minor — what a release added: a module written for 2.0 (the lower bound of api) does not get the
        # points, fields and rights of 2.1+ — otherwise its api lies, and a 2.0 hub would refuse it with "no such
        # field" rather than "update the hub"
        from core import modrun
        self.assertEqual(version.MODULE_POINTS["2.0"], ("hello", "migrate", "launch.env", "cli", "doctor"))
        self.assertEqual(version.module_floor("^2.0"), "2.0.0")
        self.assertEqual(version.module_floor(">=2.1,<3"), "2.1.0")
        self.assertEqual(version.module_floor("*"), version.MODULE_API)
        later = [p for v, pts in version.MODULE_POINTS.items() if v != "2.0" for p in pts]
        self.assertTrue({"prompt.parts", "prompt.input", "fold", "actions"} <= set(later))
        self.assertFalse(set(later) & set(version.module_points(version.module_floor("^2.0"))))
        ctx = {"name": "m", "version": "1.0.0", "capabilities": ["prompt"], "api": "^2.0"}
        with self.assertRaises(modrun.BadResult) as e:
            modrun._hello({"name": "m", "version": "1.0.0", "points": ["prompt.parts"]}, ctx)
        self.assertIn("2.1", str(e.exception))
        self.assertEqual(modrun._hello({"name": "m", "version": "1.0.0", "points": ["prompt.parts"]},
                                       {**ctx, "api": "^2.1"})["points"], ["prompt.parts"])
        # and in hello the hub does not offer them to such a module — except the optional ones (version.OPTIONAL_POINTS,
        # the Codex pool): a module of any floor may declare them when offered, with their right
        self.assertFalse((set(later) - set(version.OPTIONAL_POINTS)) & set(modrun.offered("^2.0")))
        from core import codexslot   # the Codex pool's points only where the tree has its code (the open flavor has not)
        self.assertEqual(set(version.OPTIONAL_POINTS) & set(modrun.offered("^2.0")),
                         set(version.OPTIONAL_POINTS) if codexslot.present() else set())
        self.assertIn("prompt.parts", modrun.offered("^2.1"))
        self.assertNotIn("fold", modrun.offered("^2.1"))
        for extra, since in ((dict(capabilities=["http"]), "2.1"), (dict(web="web/main.js"), "2.1"),
                             (dict(capabilities=["prompt"]), "2.1"), (dict(capabilities=["core.write"]), "2.2")):
            errs = modules.validate(manifest("m", **extra))
            self.assertTrue(any(f"module API {since}" in e for e in errs), (extra, errs))
            self.assertEqual([e for e in modules.validate(manifest("m", api=f"^{since}", **extra))
                              if "module API" in e], [], extra)


class Manifest(unittest.TestCase):
    def test_a_good_manifest(self):
        self.assertEqual(modules.validate(manifest("hello", description="example", capabilities=["cli"],
                                                   requires={"modules": {"base": "^1.0"}, "plugins": ["x"]},
                                                   optional={"modules": {"extra": ">=1"}}, env=["MY_VAR"],
                                                   budget={"hello": 1})), [])

    def test_problems_are_lines_a_person_can_fix(self):
        bad = modules.validate({"name": "Hello World", "version": "one", "api": "<2", "run": "main.py"})
        self.assertEqual(len(bad), 4, bad)
        self.assertTrue(all(isinstance(x, str) and x for x in bad))

    def test_run_is_a_list_of_words(self):
        self.assertTrue(modules.validate({**manifest("a"), "run": []}))
        self.assertTrue(modules.validate({**manifest("a"), "run": ["python3", ""]}))
        self.assertTrue(modules.validate({k: v for k, v in manifest("a").items() if k != "run"}))

    def test_fields_this_hub_does_not_call_are_refused(self):
        # in this line a module is its own process: code in the core process and entry points nobody will call are
        # a refusal, not a silent "does nothing"
        for field in ("entry", "webhook", "remote", "mode"):
            errs = modules.validate({**manifest("a"), field: "x.py"})
            self.assertTrue(any(field in e for e in errs), (field, errs))
        errs = modules.validate({**manifest("a"), "whatever": 1})
        self.assertTrue(any("whatever" in e for e in errs), errs)

    def test_env_names_that_change_what_code_runs_are_refused(self):
        for name in ("PATH", "LD_PRELOAD", "NODE_OPTIONS", "HUB_SESSION", "HUB_NESTED_DENY", "PYTHONPATH"):
            self.assertTrue(modules.validate({**manifest("a"), "env": [name]}), name)
        self.assertEqual(modules.validate({**manifest("a"), "env": ["HUB_MOD_A_TOKEN", "MY_VAR"]}), [])
        self.assertTrue(modules.validate({**manifest("a"), "env": ["HUB_MOD_B_TOKEN"]}))   # someone else's prefix

    def test_unknown_capability(self):
        self.assertTrue(modules.validate({**manifest("a"), "capabilities": ["root"]}))
        self.assertTrue(modules.validate({**manifest("a"), "capabilities": [{}, ["net"]]}))   # does not break the check
        self.assertEqual(modules.validate({**manifest("a"), "capabilities": ["net:example.com"]}), [])

    def test_budget_can_only_be_smaller(self):
        self.assertTrue(modules.validate({**manifest("a"), "budget": {"hello": 30}}))
        self.assertTrue(modules.validate({**manifest("a"), "budget": {"nope": 1}}))


class Graph(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="modgraph-")

    def put(self, name, folder=None, **man):
        d = os.path.join(self.root, folder or name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump(manifest(name, **man), f)
        return d

    def resolve(self, enabled, catalog=None, blocked=None, **cfg):
        cfg = {"modules": {"paths": [self.root], "enabled": enabled, **cfg}}
        return {r["name"]: r for r in modules.resolve(modules.discover(cfg, self.root, builtin=False), cfg,
                                                      catalog=catalog, blocked=blocked)}

    def test_external_modules_load_only_when_enabled(self):
        self.put("a")
        self.assertEqual(self.resolve([])["a"]["state"], "disabled")
        self.assertEqual(self.resolve(["a"])["a"]["state"], "ok")

    def test_load_order_is_dependencies_first_then_by_name(self):
        self.put("c", requires={"modules": {"a": "^1.0"}})
        self.put("b")
        self.put("a")
        got = modules.resolve(modules.discover({"modules": {"paths": [self.root]}}, self.root, builtin=False),
                              {"modules": {"paths": [self.root], "enabled": ["a", "b", "c"]}})
        self.assertEqual([r["name"] for r in got], ["a", "b", "c"])
        self.assertEqual([r["order"] for r in got], [0, 1, 2])

    def test_missing_wrong_version_and_disabled_dependencies_block_with_a_reason(self):
        self.put("a", requires={"modules": {"nope": "^1.0"}})
        self.put("b", requires={"modules": {"c": "^2.0"}})
        self.put("c")
        self.put("d", requires={"modules": {"e": "^1.0"}})
        self.put("e")
        r = self.resolve(["a", "b", "c", "d"])
        self.assertEqual(r["a"]["state"], "blocked")
        self.assertIn("nope", r["a"]["reason"])
        self.assertIn("found 1.0.0", r["b"]["reason"])
        self.assertIn("modules.enabled", r["d"]["reason"])
        self.assertEqual(r["c"]["state"], "ok")

    def test_a_block_spreads_to_whoever_needs_the_module(self):
        self.put("a", api="^1.0")                      # a module of another API line
        self.put("b", requires={"modules": {"a": "^1.0"}})
        self.put("c", requires={"modules": {"b": "^1.0"}})
        self.put("d")
        r = self.resolve(["a", "b", "c", "d"])
        self.assertEqual([r[n]["state"] for n in "abcd"], ["blocked", "blocked", "blocked", "ok"])
        self.assertIn("1.x", r["a"]["reason"])
        self.assertIn("a", r["b"]["reason"])

    def test_a_cycle_blocks_its_members_only(self):
        self.put("a", requires={"modules": {"b": "^1.0"}})
        self.put("b", requires={"modules": {"a": "^1.0"}})
        self.put("c")
        r = self.resolve(["a", "b", "c"])
        self.assertEqual(r["a"]["state"], "blocked")
        self.assertIn("cycle", r["a"]["reason"])
        self.assertEqual(r["b"]["state"], "blocked")
        self.assertEqual(r["c"]["state"], "ok")

    def test_optional_dependency_orders_but_never_blocks(self):
        self.put("a", optional={"modules": {"z": "^1.0", "b": "^1.0"}})
        self.put("b")
        got = [x["name"] for x in self.resolve(["a", "b"]).values() if x["state"] == "ok"]
        self.assertEqual(sorted(got), ["a", "b"])
        order = modules.resolve(modules.discover({"modules": {"paths": [self.root]}}, self.root, builtin=False),
                                {"modules": {"paths": [self.root], "enabled": ["a", "b"]}})
        self.assertEqual([x["name"] for x in order][:2], ["b", "a"])

    def test_two_modules_with_one_name_are_both_blocked(self):
        self.put("a", folder="one")
        self.put("a", folder="two")
        got = modules.resolve(modules.discover({"modules": {"paths": [self.root]}}, self.root, builtin=False),
                              {"modules": {"paths": [self.root], "enabled": ["a"]}})
        self.assertEqual([x["state"] for x in got], ["blocked", "blocked"])

    def test_required_plugins_are_checked_against_the_catalog(self):
        self.put("a", requires={"plugins": ["hub-base"]})
        self.assertEqual(self.resolve(["a"], catalog={"core:hub-base"})["a"]["state"], "ok")
        self.assertIn("hub-base", self.resolve(["a"], catalog=set())["a"]["reason"])
        # a process without a catalog does not know whether the plugin exists — the module does not load, rather than loading blind
        r = self.resolve(["a"], catalog=None)["a"]
        self.assertEqual(r["state"], "blocked")
        self.assertIn("catalog", r["reason"])

    def test_runtime_blocks_spread_like_graph_blocks(self):
        # the module did not come up (no reply to hello) — blocked with a reason right away, and so are its dependents
        self.put("a")
        self.put("b", requires={"modules": {"a": "^1.0"}})
        r = self.resolve(["a", "b"], blocked={"a": "did not answer hello"})
        self.assertEqual(r["a"]["reason"], "did not answer hello")
        self.assertEqual(r["b"]["state"], "blocked")

    def test_a_folder_that_is_gone_is_a_blocked_entry_not_a_crash(self):
        cfg = {"modules": {"paths": [os.path.join(self.root, "nope")], "enabled": ["nope"]}}
        got = modules.resolve(modules.discover(cfg, self.root, builtin=False), cfg)
        self.assertEqual(got[0]["state"], "blocked")

    def test_a_typo_in_the_section_reads_as_empty(self):
        self.assertEqual(modules.settings({"modules": {"paths": "~/x", "enabled": [1]}})["paths"], [])
        self.assertEqual(modules.settings({"modules": []}), {})


class WithoutTheSection(unittest.TestCase):
    def test_a_hub_without_modules_sees_no_modules(self):
        # a hub without a modules section — as it is now: there are no built-in modules with module.json, nothing to look for
        self.assertEqual(modules.resolve(modules.discover({}, tempfile.mkdtemp()), {}), [])


if __name__ == "__main__":
    unittest.main()
