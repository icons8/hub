"""Module API 2.10: a module without its own process — a manifest with no `run`, only the page plugin (`web`) and static
fields (core/modules.py validate, core/modrun.py Supervisor.sync). The core starts nothing for it and serves its files
itself; it shows in `hub modules` as "no process"; pause, switching off and removal work as for any module; rights that
need a process are refused with the reason."""
import tests  # noqa: F401 — keeps the machine's settings out
import json
import os
import sys
import tempfile
import unittest

from core import catalog, modcli, modpages, modrun, modstate, modules, version
from platforms import current
from tests.fakes.modstand import Stand


def static(name="look", **extra):
    return {"name": name, "version": "1.0.0", "api": "^2.10", "web": "web/main.js", **extra}


class Manifest(unittest.TestCase):
    def test_a_page_only_module_has_no_run(self):
        self.assertEqual(modules.validate(static()), [])
        self.assertEqual(modules.validate(static(description="x", provides=["look"], default_off=True,
                                                 requires={"modules": {"base": "^1.0"}})), [])

    def test_without_run_it_needs_a_page_plugin(self):
        errs = modules.validate({k: v for k, v in static().items() if k != "web"})
        self.assertTrue(any(e.startswith("run:") and "web" in e for e in errs), errs)

    def test_rights_that_need_a_process_are_refused_with_the_reason(self):
        for extra in (dict(capabilities=["cli"], commands={"x": "y"}), dict(capabilities=["http"]),
                      dict(capabilities=["core.write"], fold={"message.received": {}}),
                      dict(budget={"hello": 1}), dict(env=["MY_VAR"]), dict(snapshot={"keys": ["k"]}),
                      dict(capabilities=["model"]), dict(commands={"x": "y"}), dict(http={"paths": ["/api/x"]})):
            errs = modules.validate(static(**extra))
            self.assertTrue(any("process" in e for e in errs), (extra, errs))
        # an empty value asks for nothing, in any of these fields
        self.assertEqual(modules.validate(static(capabilities=[], env=[], commands={}, budget={})), [])

    def test_it_exists_since_the_version_that_introduced_it(self):
        errs = modules.validate(static(api="^2.9"))
        self.assertTrue(any(f"module API {version.NOPROC_SINCE}" in e for e in errs), errs)
        self.assertEqual(modules.validate(static(api=f"^{version.NOPROC_SINCE}")), [])
        # an older core cannot take it: the api range above its own is a refusal "update the hub", not a crash
        self.assertFalse(version.module_api("^2.99")[0])

    def test_a_module_with_run_is_as_before(self):
        man = {"name": "x", "version": "1.0.0", "api": "^2.0", "run": ["python3", "main.py"]}
        self.assertEqual(modules.validate(man), [])
        self.assertTrue(modules.validate({**man, "run": []}))   # a wrong run is still refused, not read as "no process"
        self.assertTrue(modules.validate({**man, "run": "main.py"}))


class Stands(unittest.TestCase):
    def stand(self):
        s = Stand(self)
        d = os.path.join(s.root, "fake")
        os.makedirs(os.path.join(d, "web"))
        with open(os.path.join(d, "web", "main.js"), "w") as f:
            f.write("export default () => {};\n")
        with open(os.path.join(d, "web", "style.css"), "w") as f:
            f.write(".x{}")
        with open(os.path.join(d, "module.json"), "w") as f:
            json.dump(static("fake"), f)
        s.hub.modules.reload(s.cfg, {})
        return s

    def test_no_process_is_asked_for_and_the_files_are_served(self):
        s = self.stand()
        sup = s.hub.modules
        self.assertEqual(sup.sync(), {})   # nothing to start: hub-up, which reads the want files, sees no module
        self.assertEqual(modrun.wants(s.state_dir), {})
        got = modpages.statics(s.state_dir)
        self.assertEqual(list(got), ["fake"])
        self.assertEqual(got["fake"]["cwd"], os.path.join(s.root, "fake"))
        self.assertTrue(got["fake"]["key"])
        sup.tick()
        self.assertEqual(sup.pending(), [])   # no session launch waits for it
        self.assertEqual(s.events("module.ready"), [])   # nobody to say hello
        self.assertEqual(s.events("module.error"), [])
        self.assertEqual(sup.watchers(), {})
        self.assertFalse(os.path.exists(modrun.slice_path(s.state_dir, "fake")))   # no process reads a slice
        self.assertTrue(modstate.web_file(s.state_dir, "fake", "main.js").endswith(os.path.join("web", "main.js")))
        self.assertIsNotNone(modstate.web_file(s.state_dir, "fake", "style.css"))
        self.assertIsNone(modstate.web_file(s.state_dir, "fake", "../module.json"))
        view = modstate.view({}, s.state_dir)
        self.assertEqual(view["plugins"], [{"name": "mod:fake", "main": "main.js", "base": "/mod/fake/web/"}])
        self.assertEqual(view["mod_versions"], {"fake": "1.0.0"})

    def test_one_want_by_name_for_a_request_per_file(self):
        s = self.stand()
        s.hub.modules.tick()
        self.assertEqual(modpages.one(s.state_dir, "fake"), modpages.statics(s.state_dir)["fake"])
        for bad in ("nope", "../fake", "", None):
            self.assertIsNone(modpages.one(s.state_dir, bad), bad)

    def test_developer_mode_does_not_apply_without_a_process(self):
        s = self.stand()
        cfg = {"modules": {**s.cfg["modules"], "dev": ["fake"]}}
        s.hub.modules.reload(cfg, {})
        self.assertEqual(s.hub.modules.devs(), {})   # nothing to restart: no scans, no fingerprint in the key
        s.hub.modules.tick()
        self.assertEqual(s.hub.modules.stamps, {})
        self.assertFalse([r for r in modules.rows(cfg, s.state_dir, s.hub.state) if r["name"] == "fake"][0]["dev"])

    def test_doctor_names_a_missing_page_plugin_file(self):
        s = self.stand()
        os.remove(os.path.join(s.root, "fake", "web", "main.js"))
        with open(catalog.config_path(s.state_dir), "w") as f:
            json.dump(s.cfg, f)
        checks = [c for c in modcli.checks(s.state_dir, [sys.executable]) if "fake" in c[1]]
        self.assertEqual([c[0] for c in checks], ["fail"], checks)
        self.assertIn("web/main.js", checks[0][2])

    def test_the_update_step_counts_it_ready_without_a_hello(self):
        from modules.update import private
        s = self.stand()
        s.hub.modules.tick()
        got = private.module_states(s.state_dir, ["fake"])
        self.assertEqual(got["fake"], {"ready": True, "status": "ok"})

    def test_hub_modules_says_no_process_and_doctor_agrees(self):
        s = self.stand()
        s.hub.modules.tick()
        rows = [r for r in modules.rows(s.cfg, s.state_dir, s.hub.state) if r["name"] == "fake"]
        self.assertEqual([(r["name"], r["state"]) for r in rows], [("fake", "static")])
        self.assertIn("static", modules.LABEL)
        text = modules.text(rows)
        self.assertNotIn("starting", text)
        from core import i18n
        self.assertIn(i18n.t(modules.LABEL["static"]), text)
        with open(catalog.config_path(s.state_dir), "w") as f:
            json.dump(s.cfg, f)
        checks = [c for c in modcli.checks(s.state_dir, [sys.executable]) if "fake" in c[1]]
        self.assertEqual([c[0] for c in checks], ["ok"], checks)
        self.assertNotIn("not ready", checks[0][2])

    def test_switching_off_and_removing_work(self):
        s = self.stand()
        sup = s.hub.modules
        sup.tick()
        self.assertEqual(list(modpages.statics(s.state_dir)), ["fake"])
        cfg = {"modules": {**s.cfg["modules"], "enabled": []}}   # switched off in modules.enabled
        sup.reload(cfg, {})
        sup.tick()
        self.assertEqual(modpages.statics(s.state_dir), {})
        self.assertIsNone(modstate.web_file(s.state_dir, "fake", "main.js"))
        self.assertEqual({r["name"]: r["state"] for r in modules.rows(cfg, s.state_dir, s.hub.state)}["fake"], "disabled")
        sup.reload(s.cfg, {})   # on again
        sup.tick()
        self.assertEqual(list(modpages.statics(s.state_dir)), ["fake"])
        current.rmtree(os.path.join(s.root, "fake"))   # removed from the disk
        sup.reload(s.cfg, {})
        sup.tick()
        self.assertEqual(modpages.statics(s.state_dir), {})

    def test_a_manifest_edit_changes_the_key_and_a_switch_to_a_process_moves_the_file(self):
        s = self.stand()
        sup = s.hub.modules
        sup.tick()
        key = modpages.statics(s.state_dir)["fake"]["key"]
        manifest = os.path.join(s.root, "fake", "module.json")
        with open(manifest, "w") as f:
            json.dump(static("fake", version="1.0.1"), f)
        sup.reload(s.cfg, {})
        sup.tick()
        self.assertNotEqual(modpages.statics(s.state_dir)["fake"]["key"], key)
        self.assertEqual(modpages.statics(s.state_dir)["fake"]["version"], "1.0.1")
        with open(manifest, "w") as f:   # the next version gets a process: the static file goes, the want appears
            json.dump({**static("fake", version="1.1.0"), "run": ["python3", "main.py"]}, f)
        sup.reload(s.cfg, {})
        sup.tick()
        self.assertEqual(modpages.statics(s.state_dir), {})
        self.assertEqual(list(modrun.wants(s.state_dir)), ["fake"])
        self.assertEqual(list(modpages.pages(s.state_dir)), ["fake"])

    def test_a_dependent_module_with_a_process_still_loads(self):
        s = self.stand()
        s.put("user", requires={"fake": "^1.0"})
        s.hub.modules.reload({"modules": {**s.cfg["modules"], "enabled": ["fake", "user"]}}, {})
        cfg = {"modules": {**s.cfg["modules"], "enabled": ["fake", "user"]}}
        self.assertEqual({r["name"]: r["state"] for r in modules.rows(cfg, s.state_dir, s.hub.state)
                          if r["name"] in ("fake", "user")}, {"fake": "static", "user": "starting"})
        self.assertEqual(list(s.hub.modules.sync()), ["user"])   # only the one with a process is started


class Folder(unittest.TestCase):
    def test_a_module_folder_with_no_run_is_found_and_read(self):
        root = tempfile.mkdtemp(prefix="noproc-")
        self.addCleanup(current.rmtree, root)
        os.makedirs(os.path.join(root, "look", "web"))
        with open(os.path.join(root, "look", "module.json"), "w") as f:
            json.dump(static("look"), f)
        found = modules.discover({"modules": {"paths": [root], "enabled": ["look"]}}, root, builtin=False)
        self.assertEqual([(f["name"], f["errors"]) for f in found], [("look", [])])


if __name__ == "__main__":
    unittest.main()
