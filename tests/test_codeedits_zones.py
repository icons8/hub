"""Every file of the core's folders is in a zone of core/codeedits.ZONES or named plain here (dev-guard spec §5.3; code
review: a hand-picked table silently leaves a new guard as plain core). Dev only (tools/test.py DEV_ONLY): it reads the
working copy, and a hub's own files must not roll an update back."""
import tests  # noqa: F401 — hide the machine's settings
import os
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FOLDERS = ("core", "hosts", "bin", "platforms", ":(glob)modules/files/*.py", ":(glob)modules/web/*.py")   # hooks/: a zone whole

PLAIN = {
    "bin/hub", "bin/hub-browser", "bin/hub-telegram", "bin/hub-up", "bin/hub-watch", "bin/hubd", "bin/hubsched",
    "core/__init__.py", "core/askexpired.py", "core/backbone.py", "core/board.py", "core/budget.py",
    "core/buscards.py", "core/buscli.py", "core/busflow.py", "core/catalog.py", "core/chain.py",
    "core/claudeauth.py", "core/clihelp.py", "core/codexcli.py", "core/codexmodel.py", "core/coretext.py", "core/delegations.py", "core/deputycli.py",
    "core/dev.py", "core/engineplugins.py", "core/executors.py", "core/flow.py", "core/forms.py", "core/gitaccess.py", "core/history.py",
    "core/httpd.py", "core/hubhelp.txt", "core/hubmod.py", "core/i18n.py", "core/identity.py", "core/instance.py", "core/instancecli.py", "core/instancejob.py", "core/kit.py", "core/kitcli.py", "core/langdata.py", "core/layout.py",
    "core/lessons.py", "core/limitresume.py", "core/lock.py", "core/mcpwatch.py", "core/migrate.py", "core/mine.py",
    "core/modclaims.py", "core/modcli.py", "core/modelcall.py", "core/modelrule.py", "core/modlaunch.py", "core/modpages.py",
    "core/modrun.py", "core/modsrc.py", "core/modstate.py", "core/modules.py", "core/news.py", "core/nolookup.py",
    "core/oldlayout.py", "core/owner.py", "core/packaged.py", "core/parking.py", "core/pluginstyle.py", "core/reader_cache.py",
    "core/refusals.py", "core/restart.py", "core/runtime.py", "core/schedule.py", "core/shortcut.py", "core/shown.py", "core/spool.py", "core/statemark.py",
    "core/subagents.py", "core/system.py", "core/tail.py", "core/taskrec.py", "core/uninstall.py", "core/usage.py", "core/uses.py",
    "core/version.py", "core/views.py", "core/workspace.py", "core/zones.py", "hosts/__init__.py", "hosts/codex.py",
    "hosts/codex_cmds.py", "hosts/codex_items.py", "hosts/codex_life.py", "hosts/codex_limits.py",
    "hosts/codex_set.py", "hosts/tmux.py", "hosts/worktree.py", "modules/files/__init__.py",
    "modules/files/paths.py", "modules/web/__init__.py", "modules/web/attach.py", "modules/web/busweb.py", "modules/web/instancesweb.py",
    "modules/web/modinfo.py", "modules/web/modmodel.py", "modules/web/modproxy.py", "modules/web/pagecache.py",
    "modules/web/preload.py", "modules/web/push.py", "modules/web/server.py", "modules/web/settings.py",
    "modules/web/stream_delta.py", "modules/web/stream_feed.py",
    "platforms/__init__.py", "platforms/posix.py", "platforms/windows.py",
}


class Zoned(unittest.TestCase):
    def files(self):
        if not os.path.exists(os.path.join(ROOT, ".git")):
            self.skipTest("not a git checkout")
        got = subprocess.run(["git", "ls-files", *FOLDERS], cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(got.returncode, 0, got.stderr)   # a broken git in a checkout is red, not a quiet skip
        self.assertTrue(got.stdout.split())
        return got.stdout.split()

    def test_every_file_of_the_core_folders_is_zoned_or_named_plain(self):
        from core import codeedits
        loose = sorted(f for f in self.files() if codeedits.zone(f) == "core" and f not in PLAIN)
        self.assertEqual(loose, [], "a new file of the core: put it into a zone of core/codeedits.ZONES or into PLAIN here")

    def test_every_path_of_the_table_is_in_the_repository(self):
        from core import codeedits, embed
        tops = [ROOT] + [top for _, top in embed.roots(ROOT)]   # the core's own or an embed's (core/embed.py)
        slots = [spec["file"] for spec in embed.SLOTS.values()]
        for key, paths in codeedits.ZONES:
            for p in paths:
                there = [os.path.isdir(os.path.join(t, p)) if p.endswith("/") else os.path.isfile(os.path.join(t, p))
                         for t in tops]
                if not any(there) and (p in embed.SHIMS or any(f.startswith(p) for f in slots)):
                    continue   # a slot's code, in no embed of this tree, or a shim that leads into one (the open flavor)
                self.assertTrue(any(there), (key, p))   # a renamed file would leave its zone

    def test_a_name_in_plain_is_not_zoned(self):
        from core import codeedits
        self.assertEqual(sorted(f for f in PLAIN if codeedits.zone(f) != "core"), [], "zoned now: drop it from PLAIN")

    def test_the_update_self_check_does_not_run_it(self):
        with open(os.path.join(ROOT, "tools", "test.py"), encoding="utf-8") as f:
            line = next(x for x in f if x.startswith("DEV_ONLY"))
        self.assertIn("tests.test_codeedits_zones.", line)


if __name__ == "__main__":
    unittest.main()
