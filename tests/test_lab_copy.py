"""A lab hub made from a copy of the live one (owner request 2026-10-04: before rolling out, create a new hub with his
journal and leave the current hub alone): live files are not touched, the lab's sessions do not continue live
conversations and do nothing on their own."""
import tests  # noqa: F401 — keeps the machine's settings out
import hashlib
import json
import os
import shutil
import tempfile
import unittest

from core import journal
from tools import lab_copy

OWNER = {"channel": "cli", "owner": True}


def digest(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


class LabCopy(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="lab-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.live = os.path.join(self.dir, "live")
        os.makedirs(os.path.join(self.live, "agents", "s-dev"))
        open(os.path.join(self.live, "agents", "s-dev", "CLAUDE.md"), "w").write("# devops\n")
        j = journal.Journal(os.path.join(self.live, "journal.jsonl"))
        j.append("session.requested", "hub", {"cwd": os.path.join(self.live, "hub-home"), "session_id": "11111111-aaaa",
                                              "kind": "hub", **OWNER})
        j.append("session.requested", "devops", {"cwd": "/k", "session_id": "22222222-bbbb", "role": "agent",
                                                 "about": "DevOps", **OWNER})
        j.append("session.started", "hub", {})
        j.append("session.started", "devops", {})
        j.append("turn.started", "devops", {"marker": None, "prompt_head": "x"})
        j.append("message.received", None, {"to": "devops", "text": "deploy X", **OWNER})
        j.append("schedule.set", "ses-weekly", {"session": "devops", "cron": "0 7 * * 1", "prompt": "SES", **OWNER})
        self.cfg = os.path.join(self.dir, "hub.json")
        json.dump({"hub": {"enabled": True, "name": "hub-core", "cwd": "/live/hub-home"},
                   "extensions": {"agent-twitter": {"path": "/x", "run": ["python3", "b.py"]}},
                   "browsers": {"mac": {"port": 9223, "risk": ["owner-data"], "profile": "/live/chrome"}}},
                  open(self.cfg, "w"))
        self.before = {p: digest(os.path.join(self.live, p)) for p in ("journal.jsonl",)}
        self.lab, self.lab_cfg = os.path.join(self.dir, "lab"), os.path.join(self.dir, "lab-cfg")
        lab_copy.main(self.live, self.cfg, self.lab, self.lab_cfg, now=5000)
        self.st = journal.load_state(self.lab)

    def test_live_files_are_untouched(self):
        self.assertEqual({p: digest(os.path.join(self.live, p)) for p in self.before}, self.before)

    def test_sessions_get_new_ids_and_start_fresh(self):
        ids = {s["session_id"] for s in self.st["sessions"].values()}
        self.assertFalse(ids & {"11111111-aaaa", "22222222-bbbb"})
        text = open(os.path.join(self.lab, "journal.jsonl")).read()
        self.assertNotIn("22222222-bbbb", text)
        self.assertFalse(any(s.get("has_history") for s in self.st["sessions"].values()))
        self.assertTrue(all("pid" in s and s["pid"] is None for s in self.st["sessions"].values()))   # hub status reads the pid

    def test_only_the_hub_is_awake_and_nothing_runs_by_itself(self):
        self.assertEqual({n: s["state"] for n, s in self.st["sessions"].items()}, {"hub": "idle", "devops": "asleep"})
        self.assertEqual({m["state"] for m in self.st["messages"].values()}, {"failed"})
        self.assertEqual(self.st["schedule"]["ses-weekly"]["state"], "paused")

    def test_replay_from_the_journal_gives_the_same_state(self):
        from core import reducer
        again = reducer.replay(journal.read(os.path.join(self.lab, "journal.jsonl")))
        self.assertEqual({n: s["state"] for n, s in again["sessions"].items()}, {"hub": "idle", "devops": "asleep"})

    def test_session_folders_inside_the_live_hub_move_to_the_lab(self):
        # lab of 2026-10-04: the lab hub worked in the live hub-home and read its CLAUDE.md, not the branch's
        self.assertEqual(self.st["sessions"]["hub"]["cwd"], os.path.join(self.lab, "hub-home"))
        self.assertEqual(self.st["sessions"]["devops"]["cwd"], "/k")              # projects stay the same

    def test_lab_settings_are_isolated(self):
        cfg = json.load(open(os.path.join(self.lab_cfg, "hub.json")))
        self.assertEqual(cfg["extensions"], {})
        self.assertEqual(cfg["update"], {"enabled": False})
        self.assertEqual(cfg["browsers"]["mac"]["port"], lab_copy.LAB_PORTS["browser"])
        self.assertEqual(cfg["hub"]["cwd"], os.path.join(self.lab, "hub-home"))
        self.assertTrue(os.path.exists(os.path.join(self.lab, "agents", "s-dev", "CLAUDE.md")))

    def test_every_browser_gets_its_own_port_and_a_profile_inside_the_lab(self):
        # whatever the browsers are called and wherever their profiles live, no lab browser touches a live one
        json.dump({"hub": {"enabled": True}, "browsers": {"work": {"port": 9223, "profile": "/live/work"},
                                                          "test": {"port": 9230}}}, open(self.cfg, "w"))
        lab, lab_cfg = os.path.join(self.dir, "lab2"), os.path.join(self.dir, "lab2-cfg")
        lab_copy.main(self.live, self.cfg, lab, lab_cfg, now=5000)
        browsers = json.load(open(os.path.join(lab_cfg, "hub.json")))["browsers"]
        self.assertEqual(set(browsers), {"work", "test"})
        self.assertEqual(len({b["port"] for b in browsers.values()} | {9223, 9230}), 4)
        for b in browsers.values():
            self.assertTrue(b["profile"].startswith(lab + os.sep), b["profile"])
            self.assertEqual(b["risk"], [])

    def test_no_lab_browser_takes_a_port_of_a_live_one(self):
        # ports were assigned in sequence from 9225: the sixth browser landed on 9230, the live default port, and the lab connected to it
        live = {n: {"kind": "local", "port": 9240 + i} for i, n in enumerate("abcd")} | {
            "e": {"kind": "local", "port": "9227"}, "vm": {"kind": "local"},
            "own": {"kind": "remote", "url": "http://127.0.0.1:9226"}}
        json.dump({"hub": {"enabled": True}, "browsers": live}, open(self.cfg, "w"))
        lab, lab_cfg = os.path.join(self.dir, "lab4"), os.path.join(self.dir, "lab4-cfg")
        lab_copy.main(self.live, self.cfg, lab, lab_cfg, now=5000)
        ports = [b["port"] for b in json.load(open(os.path.join(lab_cfg, "hub.json")))["browsers"].values()]
        self.assertEqual(len(set(ports)), 7)
        self.assertFalse({9226, 9227, 9230} & set(ports), ports)

    def test_a_remote_browser_becomes_a_local_one_of_the_lab(self):
        # a remote browser of the live hub (CDP by address, a Chrome with the owner's logins) was copied with the same
        # address and without the owner-data risk: the lab attached to the live Chrome. In the copy it is a local browser of its own
        json.dump({"hub": {"enabled": True}, "browsers": {
            "mac": {"kind": "remote", "url": "http://127.0.0.1:9223", "headers": {"Authorization": "Bearer x"},
                    "risk": ["owner-data"], "args": ["--window-size=800,600", "--user-data-dir=/live/chrome",
                                                     "--remote-debugging-port=9223"]}}}, open(self.cfg, "w"))
        lab, lab_cfg = os.path.join(self.dir, "lab3"), os.path.join(self.dir, "lab3-cfg")
        lab_copy.main(self.live, self.cfg, lab, lab_cfg, now=5000)
        text = open(os.path.join(lab_cfg, "hub.json")).read()
        b = json.loads(text)["browsers"]["mac"]
        self.assertEqual(b["kind"], "local")
        self.assertNotIn("url", b)
        self.assertNotIn("headers", b)
        self.assertNotIn("127.0.0.1:9223", text)
        self.assertNotIn("/live/chrome", text)
        self.assertEqual(b["args"], ["--window-size=800,600"])     # its own window flags stay, the live profile and port do not
        self.assertTrue(b["profile"].startswith(lab + os.sep))

    def test_refuses_to_overwrite_an_existing_lab(self):
        with self.assertRaises(SystemExit):
            lab_copy.main(self.live, self.cfg, self.lab, self.lab_cfg)


if __name__ == "__main__":
    unittest.main()
