"""A plugin was updated (`claude plugin update`, git pull of the set): sessions started with it restart with the same
conversation once they are free (12.10: after a plugin update a busy session stayed on the old version, and the hub
was restarted by hand). Off by default (plugin_update_restart in hub.json). An update is a change to the plugin itself
(another folder or the hash of its folder in the set), not to its neighbours in the set. A busy session and one with
background work wait; a service in the background does not hold. A free session with nothing to do is parked — they
do not all come up at once. One restart only: a start for another reason clears the mark."""
import tests  # noqa: F401 — keep the machine's settings out
import unittest

from core import reducer, restart

OWNER = {"channel": "cli", "owner": True}


def plugin(path, tree="t1", source="set", version=None, **kw):
    # a new commit of the set changes version for all its plugins; the plugin itself — only its tree
    return {"kind": "plugin", "path": path, "version": version or ("v1" if tree in ("t1", None) else "v2"), "source": source, "description": "", "risk": [],
            **({"tree": tree} if tree else {}), **kw}


def catalog(**over):
    tools = {"p@set": plugin("/m/set/plugins/p"), "q@set": plugin("/m/set/plugins/q"),
             "c@cache": plugin("/cache/c/1.0", tree=None), "core:base": plugin("/repo/plugins/base", source="core"),
             "jira": {"kind": "mcp", "server": {}, "risk": [], "description": ""}}
    tools.update(over)
    return tools


class PluginUpdate(unittest.TestCase):
    def setUp(self):
        self.st, self.seq, self.actions = reducer.empty(), 0, []
        self.ev("catalog.loaded", None, tools=catalog())
        for n, tools in (("a", ["p@set"]), ("b", ["c@cache"]), ("c", ["jira"])):
            self.ev("session.requested", n, cwd="/w", session_id="s-" + n, tools=tools, **OWNER)
            self.ev("session.started", n)
            self.st["sessions"][n]["has_history"] = True
        self.actions = []

    def ev(self, type, entity=None, ts=None, **data):
        self.seq += 1
        self.st, acts, why = reducer.reduce(self.st, {"seq": self.seq, "ts": ts or 1000 + self.seq, "type": type,
                                                      "entity": entity, "data": data})
        self.actions += acts
        return why

    def update(self, ts=None, on=True, **over):
        self.ev("catalog.loaded", None, ts=ts, tools=catalog(**over), **({"restart_updated": True} if on else {}))

    def did(self, do):
        return [a["session"] for a in self.actions if a["do"] == do]

    def busy(self, name):
        self.ev("message.received", None, to=name, text="task", **OWNER)
        self.ev("turn.started", name, marker=f"m{self.seq}")
        self.assertEqual(self.st["sessions"][name]["state"], "busy")
        self.actions = []

    def test_off_by_default_nothing_happens(self):
        self.update(on=False, **{"p@set": plugin("/m/set/plugins/p", tree="t2")})
        self.assertEqual(self.actions, [])
        self.assertFalse(self.st["sessions"]["a"].get("plugins_stale"))

    def test_a_free_session_with_nothing_to_do_parks_and_wakes_with_the_new_version(self):
        self.update(**{"p@set": plugin("/m/set/plugins/p", tree="t2")})
        self.assertEqual((self.did("release"), self.did("spawn")), (["a"], []))   # not all at once: the process is released
        self.assertEqual(self.st["sessions"]["a"]["state"], "parked")
        self.ev("message.received", None, to="a", text="next", **OWNER)        # the first message starts it
        self.assertEqual(self.did("spawn"), ["a"])

    def test_parking_after_an_update_is_the_ordinary_parking(self):
        # the same transition as reconcile parking and idle parking (session.parked): the delegations board learns
        # there is no process, and the handoff is cancelled — not a second copy writing state around the state machine
        from unittest import mock
        from core import delegations
        seen = []
        real = delegations.on_state
        with mock.patch.object(delegations, "on_state", lambda st, name, to, ev, ctx: (seen.append((name, to)),
                                                                                       real(st, name, to, ev, ctx))):
            self.update(**{"p@set": plugin("/m/set/plugins/p", tree="t2")})
        self.assertIn(("a", "parked"), seen)
        self.assertEqual(self.st["sessions"]["a"]["state"], "parked")

    def test_a_free_session_with_work_restarts_now(self):
        self.busy("a")
        self.ev("message.received", None, to="a", text="more", **OWNER)          # waits in the queue
        self.update(**{"p@set": plugin("/m/set/plugins/p", tree="t2")})
        self.assertEqual(self.did("release"), [])                                # busy — waits for the end of the turn
        self.ev("turn.result", "a", text="done", subtype="success")
        self.assertEqual((self.did("release"), self.did("spawn")), (["a"], ["a"]))

    def test_a_commit_to_a_neighbour_or_readme_is_not_an_update(self):
        self.update(**{"q@set": plugin("/m/set/plugins/q", tree="t2"), "p@set": plugin("/m/set/plugins/p", version="v2")})
        self.assertEqual(self.did("release"), [])

    def test_a_new_copy_folder_is_an_update(self):
        self.update(**{"c@cache": plugin("/cache/c/1.1", tree=None)})
        self.assertEqual(self.did("release"), ["b"])

    def test_background_work_waits_a_service_does_not(self):
        self.ev("session.background", "a", tasks=[{"id": "t1", "title": "e2e"}])
        self.update(**{"p@set": plugin("/m/set/plugins/p", tree="t2")})
        self.assertEqual(self.did("release"), [])
        self.assertEqual(self.st["sessions"]["a"].get("plugins_stale"), ["p@set"])
        # the same command has run for long and is silent — it is a service (a dev server): the restart does not wait
        # for it, just like a hub restart
        late = 1000 + self.seq + restart.SERVICE_AFTER + 60
        self.ev("session.info", "a", ts=late)
        self.assertEqual((self.did("release"), self.did("spawn")), (["a"], ["a"]))

    def test_core_plugins_come_with_the_hub_update(self):
        self.update(**{"core:base": plugin("/repo/plugins/base", tree="t9", source="core")})
        self.assertEqual(self.did("release"), [])

    def test_a_base_plugin_touches_every_live_session(self):
        base = plugin("/m/set/plugins/q", base=True)
        self.update(on=False, **{"q@set": base})
        self.update(**{"q@set": {**base, "tree": "t2"}})
        self.assertEqual(sorted(self.did("release")), ["a", "b", "c"])

    def test_asleep_sessions_are_left_alone(self):
        self.st["sessions"]["a"]["state"] = "asleep"
        self.update(**{"p@set": plugin("/m/set/plugins/p", tree="t2")})
        self.assertEqual(self.did("release"), [])
        self.assertFalse(self.st["sessions"]["a"].get("plugins_stale"))

    def test_one_restart_when_it_is_restarted_for_another_reason_too(self):
        self.busy("a")
        self.update(**{"p@set": plugin("/m/set/plugins/p", tree="t2")})
        self.ev("session.reload_requested", "a", **OWNER)                         # dirty: restarts after the turn anyway
        self.ev("turn.result", "a", text="done", subtype="success")
        self.assertEqual((self.did("release"), self.did("spawn")), (["a"], ["a"]))
        self.ev("session.started", "a")
        # no second start — and no second shutdown either: the mark is cleared on start, a free session with nothing
        # to do is not parked again
        self.assertEqual((self.did("release"), self.did("spawn")), (["a"], ["a"]))
        self.assertFalse(self.st["sessions"]["a"].get("plugins_stale"))

    def test_a_session_that_went_down_and_came_back_is_not_restarted_again(self):
        # hubd start: the catalog is read before the core restarts crashed processes — they come up with the new
        # version already
        self.busy("a")
        self.update(**{"p@set": plugin("/m/set/plugins/p", tree="t2")})
        self.ev("session.exited", "a")
        self.ev("session.started", "a")
        self.assertEqual(self.did("release"), [])
        self.assertFalse(self.st["sessions"]["a"].get("plugins_stale"))


class NoProcessNoMark(unittest.TestCase):
    """The "updated — will restart with them" mark exists only on a live session: with no process (parked, asleep,
    crashed) the next start picks up the new version anyway, and `hub status` does not promise a restart to a sleeping one."""

    def journal(self):
        import os
        import tempfile
        from core import journal
        self.dir = tempfile.mkdtemp(prefix="pu-st-")
        self.j = journal.Journal(os.path.join(self.dir, "journal.jsonl"))
        self.j.append("catalog.loaded", None, {"tools": catalog()})
        for n in ("a", "b"):
            self.j.append("session.requested", n, {"cwd": "/w", "session_id": "s-" + n, "tools": ["p@set"], **OWNER})
            self.j.append("session.started", n, {})
        self.j.append("message.received", None, {"to": "b", "text": "task", **OWNER})
        self.j.append("turn.started", "b", {"marker": None, "prompt_head": "task"})
        self.j.append("catalog.loaded", None, {"tools": catalog(**{"p@set": plugin("/m/set/plugins/p", tree="t2")}),
                                               "restart_updated": True})

    def status(self):
        import os
        import subprocess
        import sys
        cli = os.path.join(os.path.dirname(__file__), "..", "bin", "hub")
        return subprocess.run([sys.executable, cli, "status"], capture_output=True, text=True,
                              env={**os.environ, "HUB_STATE_DIR": self.dir}).stdout

    def test_parking_drops_the_mark(self):
        # reconcile at hubd start parks a free session with no process — a mark set by the catalog just before goes away
        st = reducer.empty()
        for seq, (type, entity, data) in enumerate((
                ("catalog.loaded", None, {"tools": catalog()}),
                ("session.requested", "a", {"cwd": "/w", "session_id": "s-a", "tools": ["p@set"], **OWNER}),
                ("session.started", "a", {})), 1):
            st, _, _ = reducer.reduce(st, {"seq": seq, "ts": 1000 + seq, "type": type, "entity": entity, "data": data})
        st["sessions"]["a"]["plugins_stale"] = ["p@set"]
        st, _, why = reducer.reduce(st, {"seq": 9, "ts": 1009, "type": "session.parked", "entity": "a",
                                         "data": {"channel": "core", "why": "restart"}})
        self.assertIsNone(why)
        self.assertEqual((st["sessions"]["a"]["state"], st["sessions"]["a"].get("plugins_stale")), ("parked", None))

    def test_a_marked_session_that_falls_asleep_is_not_promised_a_restart(self):
        # b was busy when the plugin was updated (the mark waits for the end of the turn), and then it was exited in
        # a terminal: it sleeps
        self.journal()
        self.j.append("session.ended", "b", {"reason": "prompt_input_exit"})
        out = self.status()
        self.assertIn("parked", out)        # a is parked, as after a hub restart
        self.assertNotIn("restarts with them", out)


class PluginTree(unittest.TestCase):
    """The hash of a plugin's folder — a real git in a temporary folder."""

    def repo(self, sources, under=""):
        """A repository with the set in the folder under (empty — in the root) and plugins at paths sources from the set."""
        import json
        import os
        import subprocess
        import tempfile
        self.top = tempfile.mkdtemp()
        self.root = os.path.join(self.top, under) if under else self.top
        self.git = lambda *a: subprocess.run(["git", "-C", self.top, *a], check=True, capture_output=True)  # noqa: E731
        self.git("init", "-q")
        os.makedirs(os.path.join(self.root, ".claude-plugin"))
        plugins = [{"name": os.path.basename(os.path.normpath(s)) if s != "." else "self", "source": s} for s in sources]
        with open(os.path.join(self.root, ".claude-plugin", "marketplace.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "set", "plugins": plugins}, f, ensure_ascii=False)
        for s in sources:
            os.makedirs(os.path.join(self.root, s, ".claude-plugin"), exist_ok=True)
            self.write(os.path.join(s, ".claude-plugin", "plugin.json"), "{}")
        self.commit()

    def write(self, rel, text, base=None):
        import os
        path = os.path.join(base or self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self):
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")

    def build(self):
        from core import catalog
        return catalog.build({"sets": [{"name": "set", "path": self.root}]})

    def test_a_commit_to_a_neighbour_keeps_the_plugins_tree(self):
        self.repo(["./plugins/p", "./plugins/q"])
        before = self.build()
        self.write("plugins/q/README.md", "new")
        self.commit()
        after = self.build()
        self.assertTrue(before["p@set"]["tree"])
        self.assertEqual(before["p@set"]["tree"], after["p@set"]["tree"])
        self.assertNotEqual(before["q@set"]["tree"], after["q@set"]["tree"])

    def test_a_set_in_a_subfolder_of_a_bigger_repo(self):
        # the plugin is the whole set (source "."), and the set is a folder of a bigger repository: the hash is of its
        # folder, not of the whole repository
        self.repo(["."], under="tools/set")
        before = self.build()
        self.write("README.md", "other", base=self.top)   # a commit outside the set's folder
        self.commit()
        after = self.build()
        self.assertTrue(before["self@set"]["tree"])
        self.assertEqual(before["self@set"]["tree"], after["self@set"]["tree"])
        self.write("skills/x.md", "new")                 # an edit of the set itself — an update
        self.commit()
        self.assertNotEqual(after["self@set"]["tree"], self.build()["self@set"]["tree"])

    def test_a_plugin_folder_named_not_in_latin(self):
        # git without -z returns such paths quoted with \ooo escapes — they did not match the path from
        # marketplace.json, so there was no hash
        name = "".join(chr(c) for c in (0x043a, 0x0430, 0x0440, 0x0442, 0x044b))   # "maps" in Russian letters
        self.repo([f"./plugins/{name}"])
        before = self.build()
        self.write(f"plugins/{name}/README.md", "new")
        self.commit()
        after = self.build()
        self.assertTrue(before[f"{name}@set"].get("tree"))
        self.assertNotEqual(before[f"{name}@set"]["tree"], after[f"{name}@set"]["tree"])


if __name__ == "__main__":
    unittest.main()
