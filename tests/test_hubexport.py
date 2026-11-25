"""`hub export` / `hub import` (core/hubexport.py, spec d2789272): a hub of one made-up machine into an archive and onto
another (another home folder), with the paths, the repos, the Claude memory, the secrets and the undo. Everything lives
in a temp folder: the old machine's home, state, settings and Claude folders, the new machine's, a bare repository for
the remote; the hub is "stopped" and "started" by stubs. All values are made up."""
import tests  # noqa: F401 — keep the machine's settings out
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import platforms
from core import hubexport, pathmap, reducer
from hosts import worktree

MARK = "probe-secret-9f3a7c"           # a secret value: it must never be in the open part of an archive
PASSWORD = "correct horse battery"
GIT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.test", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def need_lib():
    try:
        from core import cryptolib
        cryptolib.aesgcm()
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt  # noqa: F401
        return None
    except Exception as e:  # noqa: BLE001
        return str(e)


LIB_WHY = need_lib()
needs_lib = unittest.skipIf(LIB_WHY, f"no cryptography: {LIB_WHY}")


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, env={**os.environ, **GIT})


def tree(root):
    """{relative path: bytes} of a folder (absent — {})."""
    out = {}
    for r, dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(r, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
    return out


def snapshot(seq, state):
    return {"seq": seq, "ts": 1790000000.0, "src": "core", "type": "core.snapshot", "entity": None,
            "data": {"why": "rotate", "state": state}}


class Machines(unittest.TestCase):
    """An old machine with a hub on it, a new machine with a fresh hub."""

    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix="hubexport-"))
        self.addCleanup(platforms.current.rmtree, self.dir)
        self.old = self.machine("old")
        self.new = self.machine("new")
        self.remote = os.path.join(self.dir, "remote.git")
        self.keys = {}
        k = mock.patch("platforms.current.secret_key", self.fake_key)
        k.start()
        self.addCleanup(k.stop)
        d = mock.patch("platforms.current.drop_secret_key", self.fake_drop)
        d.start()
        self.addCleanup(d.stop)
        f = mock.patch("platforms.current.secret_key_file", lambda folder, platform=None: None)
        f.start()
        self.addCleanup(f.stop)
        self.use(self.old)
        self.build_old()

    def fake_key(self, account, folder, create=False, platform=None, run=None, value=None):
        """The OS key store of the test: one key per (settings folder, account)."""
        k = (folder, account)
        if k not in self.keys and create:
            self.keys[k] = value if value is not None else os.urandom(32)
        return self.keys.get(k)

    def fake_drop(self, account, folder, platform=None, run=None):
        return self.keys.pop((folder, account), None) is not None

    def machine(self, name):
        home = os.path.join(self.dir, name, "home")
        m = {"home": home, "state": os.path.join(home, ".hub-core"), "conf": os.path.join(home, ".config", "hub-core"),
             "claude": os.path.join(home, ".claude")}
        for k in ("state", "conf", "claude"):
            os.makedirs(m[k])
        return m

    def use(self, m):
        """Run as the hub of machine m: its folders in the environment."""
        p = mock.patch.dict(os.environ, {"HUB_STATE_DIR": m["state"], "HUB_CONFIG_DIR": m["conf"],
                                         "CLAUDE_CONFIG_DIR": m["claude"], "HOME": m["home"]})
        p.start()
        self.addCleanup(p.stop)
        os.environ.pop("HUB_CONFIG", None)   # hub.json is the machine's own (the patch puts the tests' one back)

    def write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)

    def build_old(self):
        o = self.old
        # the remote and the agent's repo cloned from it, with a commit pushed
        git(self.dir, "init", "--bare", "-b", "main", self.remote)
        self.repo = os.path.join(o["home"], "Code", "maps")
        git(self.dir, "clone", self.remote, self.repo)
        self.write(os.path.join(self.repo, "a.txt"), "a")
        git(self.repo, "add", "a.txt")
        git(self.repo, "commit", "-m", "a")
        git(self.repo, "push", "origin", "HEAD:main")
        self.commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True,
                                     text=True).stdout.strip()
        copy = os.path.join(self.repo, ".claude", "worktrees", "maps-2")
        os.makedirs(copy)
        st = reducer.empty()
        st["sessions"] = {
            "hub": {"name": "hub", "state": "idle", "kind": "hub", "role": "worker", "cwd": os.path.join(o["state"],
                                                                                                      "hub-home")},
            "maps": {"name": "maps", "state": "idle", "kind": "work", "role": "agent", "cwd": self.repo,
                     "session_id": "sid-maps", "profile": "p-maps"},
            "maps-2": {"name": "maps-2", "state": "idle", "kind": "work", "role": "executor", "cwd": copy,
                       "raised_by": "maps", "worktree": {"path": copy, "repo": self.repo}},
            # a direction's executor (a backlog): the core does not stop it — it is named as a folder to bring back
            "alpha": {"name": "alpha", "state": "idle", "kind": "work", "role": "executor", "cwd": copy + "-alpha",
                      "raised_by": "hub", "backlog": "alpha", "worktree": {"path": copy + "-alpha", "repo": self.repo}},
            "gone": {"name": "gone", "state": "removed", "kind": "work", "role": "agent",
                     "cwd": os.path.join(o["home"], "x")},
        }
        st["schedule"] = {"nightly": {"state": "active", "command": f"sh {o['home']}/bin/report.sh --x",
                                      "session": "maps"}}
        events = [snapshot(1, st), {"seq": 2, "ts": 1790000001.0, "src": "core", "type": "core.started",
                                    "entity": None, "data": {"pid": 1}}]
        self.write(os.path.join(o["state"], "journal.jsonl"), "".join(json.dumps(e) + "\n" for e in events))
        self.write(os.path.join(o["state"], "agents", "p-maps", "CLAUDE.md"), f"# maps\nlives in {o['home']}/Code\n")
        self.write(os.path.join(o["state"], "agents", "p-maps", "notes.md"), "small notes")
        self.write(os.path.join(o["state"], "agents", "p-maps", "samples", "big.bin"), b"\0" * (hubexport.CAP + 1))
        self.write(os.path.join(o["state"], "directions", "alpha", "direction.json"),
                   json.dumps({"repo": self.repo}))
        self.write(os.path.join(o["state"], "directions", "alpha", "backlog", "tasks", "t1.md"), "x" * (2 << 20))
        self.write(os.path.join(o["state"], "tasks", "d1.md"), "task")
        self.write(os.path.join(o["state"], "specs", "d1", "spec.md"), "spec")
        self.write(os.path.join(o["state"], "attachments", "a.png"), b"png")
        self.write(os.path.join(o["state"], "moddata", "accounts", "status.json"), json.dumps({"t": MARK}))
        self.write(os.path.join(o["state"], "moddata", "other", "data.json"), "{}")
        self.write(os.path.join(o["state"], "mystery", "token.txt"), MARK)      # a folder the core does not know
        self.write(os.path.join(o["state"], "logs", "hub-up.log"), "log")
        self.write(os.path.join(o["state"], "migrated"), "")
        self.write(os.path.join(o["state"], "hub-home", "notes.tsv"), "a\tb")
        self.write(os.path.join(o["conf"], "contacts.json"), json.dumps([{"name": "Ana"}]))
        self.write(os.path.join(o["conf"], "accounts.json"), json.dumps({"accounts": [{"token": MARK}]}))
        self.write(os.path.join(o["conf"], "telegram.json"), json.dumps({"token": MARK}))
        self.write(os.path.join(o["conf"], "bus", "key"), MARK)
        self.cfg = {
            "projects": {"maps": {"path": self.repo}},
            "mcp": {"db": {"server": {"command": "/bin/sh", "env": {"API_TOKEN": MARK, "PATH": "/usr/bin"}}},
                    "web": {"server": {"command": "/bin/sh", "args": [f"--token={MARK}"]}},
                    "plain": {"server": {"command": "/bin/sh", "args": ["-c", "true"]}}},
            "claude_config_dir": "",
            "tailnet": {"name": "old-machine"},   # this machine's own: never onto another
            "language": "en",
        }
        self.write(os.path.join(o["conf"], "hub.json"), json.dumps(self.cfg))
        projects = os.path.join(o["claude"], "projects", worktree.slug(self.repo))
        self.write(os.path.join(projects, "memory", "MEMORY.md"), "- remember")
        self.write(os.path.join(projects, "sid-maps.jsonl"), '{"x":1}\n')

    # --- helpers ---

    def export(self, **kw):
        self.use(self.old)
        out = os.path.join(self.dir, "out", "hub.tar.gz")
        return hubexport.export(self.old["state"], self.old["conf"], out, home=self.old["home"], **kw)

    def run_import(self, path, **kw):
        self.use(self.new)
        self.stops, self.starts = [], []
        kw.setdefault("stop", lambda: self.stops.append(1) or True)
        kw.setdefault("start", lambda: self.starts.append(1))
        return hubexport.import_archive(self.new["state"], self.new["conf"], path, home=self.new["home"],
                                        claude=self.new["claude"], **kw)

    def members(self, path):
        with tarfile.open(path, "r:gz") as tar:
            return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}


class Export(Machines):
    def test_what_goes_in(self):
        got = self.export()
        m = got["manifest"]
        names = set(self.members(got["path"]))
        self.assertIn("state/journal.jsonl", names)
        self.assertIn("state/agents/p-maps/CLAUDE.md", names)
        self.assertIn("state/agents/p-maps/notes.md", names)
        self.assertNotIn("state/agents/p-maps/samples/big.bin", names)   # an agent's working file over the cap
        self.assertIn("state/directions/alpha/backlog/tasks/t1.md", names)   # the cap is only in agents/
        self.assertIn("state/tasks/d1.md", names)
        self.assertIn("state/hub-home/notes.tsv", names)
        self.assertIn("state/migrated", names)
        self.assertNotIn("state/attachments/a.png", names)
        self.assertNotIn("state/logs/hub-up.log", names)
        self.assertNotIn("state/mystery/token.txt", names)
        self.assertIn("config/contacts.json", names)
        self.assertIn(f"claude/{worktree.slug(self.repo)}/memory/MEMORY.md", names)
        self.assertNotIn(f"claude/{worktree.slug(self.repo)}/sid-maps.jsonl", names)
        self.assertEqual(list(self.members(got["path"]))[-1], hubexport.MANIFEST)
        self.assertEqual([u["name"] for u in m["unknown"]], ["mystery"])
        self.assertEqual(m["counts"]["agents"], 1)
        self.assertEqual(m["repos"][0]["path"], self.repo)
        self.assertEqual(m["repos"][0]["commit"], self.commit)
        self.assertEqual(sorted(m["sessions"]), ["alpha", "hub", "maps", "maps-2"])   # a removed one is not asked about

    def test_full_takes_the_rest(self):
        names = set(self.members(self.export(full=True)["path"]))
        for n in ("state/agents/p-maps/samples/big.bin", "state/attachments/a.png", "state/mystery/token.txt",
                  f"claude/{worktree.slug(self.repo)}/sid-maps.jsonl"):
            self.assertIn(n, names)

    def test_no_secret_in_the_open(self):
        """T-3: no flag and --full — the secret is nowhere but the unknown folder --full takes (it says so)."""
        for full in (False, True):
            got = self.members(self.export(full=full)["path"])
            hits = sorted(n for n, data in got.items() if MARK.encode() in data)
            self.assertEqual(hits, ["state/mystery/token.txt"] if full else [], full)
        cfg = json.loads(self.members(self.export()["path"])["config/hub.json"])
        self.assertEqual(cfg["mcp"]["db"]["server"]["env"], {"API_TOKEN": hubexport.CUT, "PATH": "/usr/bin"})
        self.assertEqual(cfg["mcp"]["web"], hubexport.CUT_ENTRY)   # a secret in its command line: the whole entry
        self.assertEqual(cfg["mcp"]["plain"], self.cfg["mcp"]["plain"])

    def test_refusals(self):
        with self.assertRaises(hubexport.Refused) as e:
            hubexport.export(self.old["state"], self.old["conf"], os.path.join(self.old["state"], "x.tar.gz"))
        self.assertEqual(e.exception.key, "cli.export.inside")
        other = os.path.join(self.dir, "notes.txt")
        self.write(other, "mine")
        with self.assertRaises(hubexport.Refused) as e:
            hubexport.export(self.old["state"], self.old["conf"], other)
        self.assertEqual(e.exception.key, "cli.export.not_archive")
        with open(other) as f:
            self.assertEqual(f.read(), "mine")

    def test_a_week_round(self):
        """T-11: an explicit name of an archive is replaced — eight nightly runs over seven names leave seven files."""
        folder = os.path.join(self.dir, "nightly")
        for n in range(8):
            hubexport.export(self.old["state"], self.old["conf"], os.path.join(folder, f"hub-{n % 7}.tar.gz"))
        self.assertEqual(len(os.listdir(folder)), 7)

    def test_an_agent_born_while_files_were_read(self):
        """T-2: the journal knows an agent whose folder the file pass did not see — its profile comes along."""
        real = hubexport.read_journal

        def later(state):
            self.write(os.path.join(state, "agents", "p-late", "CLAUDE.md"), "# late")
            data = real(state)
            st = hubexport.journal_state(data)[0]
            st["sessions"]["late"] = {"name": "late", "state": "idle", "role": "agent", "kind": "work",
                                      "cwd": self.repo, "profile": "p-late"}
            return data + (json.dumps(snapshot(3, st)) + "\n").encode()
        with mock.patch.object(hubexport, "plan_state", wraps=hubexport.plan_state), \
                mock.patch.object(hubexport, "read_journal", later):
            got = self.export()
        self.assertIn("state/agents/p-late/CLAUDE.md", self.members(got["path"]))

    def test_a_file_that_keeps_changing_is_left_out(self):
        real = hubexport._read_stable

        def torn(path, tries=4):
            return None if path.endswith("notes.md") else real(path, tries)
        with mock.patch.object(hubexport, "_read_stable", torn):
            got = self.export()
        self.assertNotIn("state/agents/p-maps/notes.md", self.members(got["path"]))
        self.assertEqual(got["manifest"]["changing"], ["state/agents/p-maps/notes.md"])


class Import(Machines):
    def test_round_trip(self):
        """T-1: the new machine has the agents, their profiles byte for byte, the directions with this machine's paths,
        the repo cloned at its commit, the memory, and the mark for hubd."""
        path = self.export()["path"]
        got = self.run_import(path)
        n, o = self.new, self.old
        self.assertEqual((self.stops, self.starts), ([1], [1]))
        with open(os.path.join(o["state"], "journal.jsonl"), "rb") as a, \
                open(os.path.join(n["state"], "journal.jsonl"), "rb") as b:
            self.assertEqual(a.read(), b.read())   # the journal is never rewritten
        self.assertEqual(tree(os.path.join(n["state"], "agents", "p-maps")),
                         {k: v for k, v in tree(os.path.join(o["state"], "agents", "p-maps")).items()
                          if not k.startswith("samples")})
        new_repo = os.path.join(n["home"], "Code", "maps")
        with open(os.path.join(n["state"], "directions", "alpha", "direction.json")) as f:
            self.assertEqual(json.load(f)["repo"], new_repo)
        self.assertEqual(subprocess.run(["git", "rev-parse", "HEAD"], cwd=new_repo, capture_output=True,
                                        text=True).stdout.strip(), self.commit)
        with open(os.path.join(n["conf"], "hub.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["projects"]["maps"]["path"], new_repo)
        self.assertNotIn("tailnet", cfg)   # the new machine has none: not the old one's
        self.assertEqual(cfg["mcp"]["db"]["server"]["env"], {"PATH": "/usr/bin"})
        self.assertNotIn("web", cfg["mcp"])
        self.assertEqual(sorted(".".join(map(str, g["path"])) for g in got["gone"]),
                         ["mcp.db.server.env.API_TOKEN", "mcp.web"])
        self.assertTrue(os.path.isfile(os.path.join(n["claude"], "projects", worktree.slug(new_repo), "memory",
                                                    "MEMORY.md")))
        with open(os.path.join(n["state"], hubexport.MARK)) as f:
            mark = json.load(f)
        self.assertEqual(mark["missing"], ["maps-2"])   # the executor's copy did not come along (not the direction's)
        self.assertEqual([x["name"] for x in got["missing"] if not x["copy"]], ["alpha"])
        self.assertIn([o["home"], n["home"]], mark["paths"])
        self.assertEqual(got["profile_mentions"], 1)
        lines = hubexport.import_lines(got, n["state"])
        self.assertTrue(any("maps-2" in x for x in lines))

    def test_the_fold_moves_the_paths(self):
        """T-7/T-8: core.imported moves the state's paths; the texts of messages stay."""
        path = self.export()["path"]
        self.run_import(path)
        with open(os.path.join(self.new["state"], "journal.jsonl")) as f:
            events = [json.loads(x) for x in f if x.strip()]
        with open(os.path.join(self.new["state"], hubexport.MARK)) as f:
            mark = json.load(f)
        events.append({"seq": 3, "ts": 1790000002.0, "src": "core", "type": "core.imported", "entity": None,
                       "data": {k: mark[k] for k in ("paths", "from_os", "to_os", "archive", "made")}})
        st = reducer.replay(events)
        self.assertEqual(st["sessions"]["maps"]["cwd"], os.path.join(self.new["home"], "Code", "maps"))
        self.assertEqual(st["schedule"]["nightly"]["command"],
                         f"sh {os.path.join(self.new['home'], 'bin', 'report.sh')} --x")   # this machine's separators
        flat = json.dumps(st)
        self.assertNotIn(self.old["home"] + os.sep, flat.replace(json.dumps(self.old["home"])[1:-1], self.old["home"]))

    def test_not_fresh_is_refused(self):
        path = self.export()["path"]
        self.write(os.path.join(self.new["conf"], "contacts.json"), json.dumps([{"name": "Bo"}]))
        before = tree(self.new["home"])
        with self.assertRaises(hubexport.Refused) as e:
            self.run_import(path)
        self.assertEqual(e.exception.key, "cli.import.not_fresh")
        self.assertEqual(tree(self.new["home"]), before)
        self.assertEqual(self.stops, [])
        got = self.run_import(path, replace=True)
        self.assertTrue(os.path.isdir(got["aside"]))

    def test_damaged_and_foreign_archives(self):
        path = self.export()["path"]
        members = self.members(path)
        for bad, key in ((dict(members, **{"state/tasks/d1.md": b"changed"}), "cli.import.damaged"),
                         (dict(members, **{"state/../escape": b"x"}), "cli.import.bad_member")):
            broken = os.path.join(self.dir, "broken.tar.gz")
            with tarfile.open(broken, "w:gz") as tar:
                for name, data in bad.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            before = tree(self.new["home"])
            with self.assertRaises(hubexport.Refused) as e:
                self.run_import(broken)
            self.assertEqual(e.exception.key, key)
            self.assertEqual(tree(self.new["home"]), before)
            self.assertFalse(os.path.exists(os.path.join(self.dir, "escape")))

    def test_newer_code_is_refused(self):
        """An event type this code does not fold — the archive of a newer core."""
        with open(os.path.join(self.old["state"], "journal.jsonl"), "a") as f:
            f.write(json.dumps({"seq": 3, "ts": 1790000002.0, "src": "core", "type": "core.from_the_future",
                                "entity": None, "data": {}}) + "\n")
        path = self.export()["path"]
        with self.assertRaises(hubexport.Refused) as e:
            self.run_import(path)
        self.assertEqual(e.exception.key, "cli.import.unknown")
        self.assertIn("core.from_the_future", e.exception.params["types"])

    def test_a_failure_puts_everything_back(self):
        """T-6: a failure after the hub was stopped — everything as it was, the hub started again."""
        path = self.export()["path"]
        self.write(os.path.join(self.new["state"], "journal.jsonl"), "fresh\n")
        self.write(os.path.join(self.new["conf"], "hub.json"), "{}")
        before = {k: tree(self.new[k]) for k in ("state", "conf", "claude")}
        with mock.patch.object(hubexport, "_put_claude", side_effect=OSError("disk full")):
            with self.assertRaises(hubexport.Refused) as e:
                self.run_import(path)
        self.assertEqual(e.exception.key, "cli.import.failed")
        self.assertEqual(self.starts, [1])
        self.assertEqual({k: tree(self.new[k]) for k in ("state", "conf", "claude")}, before)
        self.assertTrue(os.path.isfile(os.path.join(e.exception.params["failed"], "state", "journal.jsonl")))

    def test_undo(self):
        path = self.export()["path"]
        self.write(os.path.join(self.new["state"], "journal.jsonl"), "fresh\n")
        self.write(os.path.join(self.new["conf"], "hub.json"), "{}")
        before_state, before_conf = tree(self.new["state"]), tree(self.new["conf"])
        before_claude = tree(self.new["claude"])
        self.run_import(path)
        # the imported hub worked a while: names it made itself go too, not into the hub that comes back
        self.write(os.path.join(self.new["state"], "spool", "e1.json"), "{}")
        self.write(os.path.join(self.new["state"], "boards.json"), "{}")
        aside, undone = hubexport.undo(self.new["state"], self.new["conf"], stop=lambda: True, start=lambda: None)
        self.assertEqual(tree(self.new["state"]), before_state)
        self.assertEqual(tree(self.new["conf"]), before_conf)
        self.assertEqual(tree(self.new["claude"]), before_claude)
        self.assertTrue(os.path.isfile(os.path.join(undone, "state", "journal.jsonl")))   # kept, not deleted
        with self.assertRaises(hubexport.Refused):
            hubexport.undo(self.new["state"], self.new["conf"])

    def test_a_hub_that_did_not_stop_is_started_again(self):
        path = self.export()["path"]
        with self.assertRaises(hubexport.Refused) as e:
            self.run_import(path, stop=lambda: False)
        self.assertEqual(e.exception.key, "cli.import.not_stopped")
        self.assertEqual(self.starts, [1])

    def test_a_failed_undo_leaves_the_hub_stopped(self):
        """The rollback itself fails: the hub is not started on a half-swapped folder; the steps let --undo go on."""
        path = self.export()["path"]
        real = hubexport._move
        calls = []

        def move(src, dst):
            calls.append(dst)
            if ".failed-import-" in dst and len(calls) > 1:
                raise OSError("the disk went away")
            return real(src, dst)
        with mock.patch.object(hubexport, "_put_claude", side_effect=OSError("disk full")), \
                mock.patch.object(hubexport, "_move", move):
            with self.assertRaises(hubexport.Refused) as e:
                self.run_import(path)
        self.assertEqual(e.exception.key, "cli.import.undo_failed")
        self.assertEqual(self.starts, [])
        hubexport.undo(self.new["state"], self.new["conf"], stop=lambda: True, start=lambda: None)
        self.assertFalse(os.path.exists(os.path.join(self.new["state"], "agents")))

    def test_the_password_comes_from_a_terminal_only(self):
        with mock.patch("sys.stdin", io.StringIO("pw-pw-pw-pw-pw\npw-pw-pw-pw-pw\n")):
            with self.assertRaises(hubexport.Refused) as e:
                hubexport.ask_password()
        self.assertEqual(e.exception.key, "cli.export.password_terminal")

    def test_settings_that_keep_changing_stop_the_export(self):
        real = hubexport._read_stable
        with mock.patch.object(hubexport, "_read_stable",
                               lambda p, tries=4: None if p.endswith("hub.json") else real(p, tries)):
            with self.assertRaises(hubexport.Refused) as e:
                self.export()
        self.assertEqual(e.exception.key, "cli.export.config_busy")

    def test_secrets_from_a_session_are_refused(self):
        """A session may export, not seal the secrets under a password of its own choice."""
        import sys
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = {**os.environ, "HUB_SESSION": "maps"}
        env.pop("HUB_CONFIG", None)
        r = subprocess.run([sys.executable, os.path.join(repo, "bin", "hub"), "export",
                            os.path.join(self.dir, "x.tar.gz"), "--secrets"], capture_output=True, text=True,
                           env=env, input="pw-pw-pw-pw-pw\npw-pw-pw-pw-pw\n", timeout=120)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--secrets", r.stderr + r.stdout)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "x.tar.gz")))

    def test_undo_keeps_what_the_import_did_not_move(self):
        """An import without secrets over a hub with its own bot: the undo does not take the bot away."""
        path = self.export()["path"]
        self.write(os.path.join(self.new["conf"], "telegram.json"), '{"own": true}')
        self.run_import(path, replace=True)
        hubexport.undo(self.new["state"], self.new["conf"], stop=lambda: True, start=lambda: None)
        with open(os.path.join(self.new["conf"], "telegram.json")) as f:
            self.assertEqual(f.read(), '{"own": true}')

    def test_a_second_undo_goes_on_from_where_the_first_stopped(self):
        path = self.export()["path"]
        self.write(os.path.join(self.new["state"], "journal.jsonl"), "fresh\n")
        self.write(os.path.join(self.new["state"], "agents", "p-own", "CLAUDE.md"), "# own")
        before = tree(self.new["state"])
        self.run_import(path, replace=True)
        real, failed = hubexport._move, []

        def move(src, dst):   # putting back the agents fails once — the journal is back already (reverse order)
            if dst == os.path.join(self.new["state"], "agents") and not failed:
                failed.append(1)
                raise OSError("busy")
            return real(src, dst)
        with mock.patch.object(hubexport, "_move", move):
            with self.assertRaises(hubexport.Refused) as e:
                hubexport.undo(self.new["state"], self.new["conf"], stop=lambda: True, start=lambda: None)
        self.assertEqual(e.exception.key, "cli.import.undo_failed")
        hubexport.undo(self.new["state"], self.new["conf"], stop=lambda: True, start=lambda: None)
        self.assertEqual(tree(self.new["state"]), before)

    def test_a_claude_file_changed_since_is_kept(self):
        path = self.export()["path"]
        self.run_import(path)
        memory = os.path.join(self.new["claude"], "projects", worktree.slug(os.path.join(self.new["home"], "Code",
                                                                                          "maps")), "memory",
                              "MEMORY.md")
        self.write(memory, "- remember\n- a lesson learned after the move")
        aside, undone = hubexport.undo(self.new["state"], self.new["conf"], stop=lambda: True, start=lambda: None)
        self.assertFalse(os.path.exists(memory))
        kept = [f for f in os.listdir(os.path.join(undone, "claude"))]
        self.assertEqual(len(kept), 1)
        with open(os.path.join(undone, "claude", kept[0])) as f:
            self.assertIn("a lesson learned", f.read())

    def test_this_machines_entries_never_come_from_an_archive(self):
        path = self.export()["path"]
        members = self.members(path)
        for name in ("state/instance.json", "state/code/x.py", "state/hubd.lock", f"state/{hubexport.MARK}"):
            broken = os.path.join(self.dir, "broken.tar.gz")
            with tarfile.open(broken, "w:gz") as tar:
                for n, data in dict(members, **{name: b"x"}).items():
                    info = tarfile.TarInfo(n)
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
            with self.assertRaises(hubexport.Refused) as e:
                self.run_import(broken)
            self.assertEqual(e.exception.key, "cli.import.bad_member", name)

    def test_no_clone_lists_the_repos(self):
        got = self.run_import(self.export()["path"], no_clone=True)
        self.assertEqual([c["outcome"] for c in got["clones"]], ["listed"])
        self.assertFalse(os.path.exists(os.path.join(self.new["home"], "Code", "maps")))


@needs_lib
class Secrets(Machines):
    def setUp(self):
        super().setUp()
        self.vault_key = os.urandom(32)
        self.keys[(self.old["conf"], "main")] = self.vault_key
        self.write(os.path.join(self.old["state"], "secrets", "vault.json"), json.dumps({"v": 1, "items": {}}))

    def test_sealed_and_back(self):
        got = self.export(password=PASSWORD)
        members = self.members(got["path"])
        self.assertIn(hubexport.SECRETS, members)
        self.assertEqual(sorted(n for n, d in members.items() if MARK.encode() in d), [])
        self.run_import(got["path"], password=lambda: PASSWORD)
        with open(os.path.join(self.new["conf"], "hub.json")) as f:
            cfg = json.load(f)
        self.assertEqual(cfg["mcp"], self.cfg["mcp"])   # whole, from secrets.bin
        for name in ("accounts.json", "telegram.json", os.path.join("bus", "key")):
            with open(os.path.join(self.new["conf"], name)) as f:
                self.assertIn(MARK, f.read())
        self.assertEqual(self.keys[(self.new["conf"], "main")], self.vault_key)

    def test_wrong_password_changes_nothing(self):
        path = self.export(password=PASSWORD)["path"]
        before = tree(self.new["home"])
        with self.assertRaises(hubexport.Refused) as e:
            self.run_import(path, password=lambda: "not the password")
        self.assertEqual(e.exception.key, "cli.import.password_wrong")
        self.assertEqual(tree(self.new["home"]), before)
        got = self.run_import(path, no_secrets=True)
        self.assertFalse(got["secrets"])

    def test_a_secrets_blob_of_another_archive_does_not_open(self):
        blob = hubexport.seal(self.old["state"], {"x": b"y"}, PASSWORD, "id-one")
        with self.assertRaises(hubexport.Refused):
            hubexport.unseal(self.old["state"], blob, PASSWORD, "id-two")
        self.assertEqual(hubexport.unseal(self.old["state"], blob, PASSWORD, "id-one"), {"x": b"y"})

    def test_a_failure_before_the_key_went_aside_keeps_it(self):
        """The key store refuses to keep the old key aside (a locked Keychain): the old key stays, nothing is dropped."""
        own = os.urandom(32)
        self.keys[(self.new["conf"], "main")] = own
        real = self.fake_key

        def refusing(account, folder, create=False, **kw):
            if ".before-import-" in account and create:
                raise OSError("security: the Keychain is locked")
            return real(account, folder, create, **kw)
        path = self.export(password=PASSWORD)["path"]
        with mock.patch("platforms.current.secret_key", refusing):
            with self.assertRaises(hubexport.Refused) as e:
                self.run_import(path, password=lambda: PASSWORD, replace=True)
        self.assertEqual(e.exception.key, "cli.import.failed")
        self.assertEqual(self.keys[(self.new["conf"], "main")], own)

    def test_undo_brings_back_the_old_key(self):
        own = os.urandom(32)
        self.keys[(self.new["conf"], "main")] = own
        self.run_import(self.export(password=PASSWORD)["path"], password=lambda: PASSWORD, replace=True)
        self.assertEqual(self.keys[(self.new["conf"], "main")], self.vault_key)
        hubexport.undo(self.new["state"], self.new["conf"], stop=lambda: True, start=lambda: None)
        self.assertEqual(self.keys[(self.new["conf"], "main")], own)


class Paths(unittest.TestCase):
    def test_table(self):
        t = pathmap.Table([["/Users/x", "/home/x"], ["/Users/x/.hub-core", "/srv/hub"]])
        self.assertEqual(t.move("/Users/x/Code/maps"), "/home/x/Code/maps")
        self.assertIsNone(t.move("/Users/xy/a"))
        self.assertEqual(t.move("/Users/x/.hub-core/agents"), "/srv/hub/agents")
        self.assertIsNone(t.move("/Volumes/Data/maps"))
        self.assertEqual(t.words('cd /Users/x/a && "/Users/x/b c" --x=/Users/x/q'),
                         ('cd /home/x/a && "/home/x/b c" --x=/Users/x/q', True))

    def test_between_systems(self):
        w = pathmap.Table([["/home/x", "C:\\Users\\x"]], False, True)
        self.assertEqual(w.move("/home/x/Code/maps"), "C:\\Users\\x\\Code\\maps")
        b = pathmap.Table([["C:\\Users\\X", "/home/x"]], True, False)
        self.assertEqual(b.move("c:\\users\\x\\Code/maps"), "/home/x/Code/maps")
        self.assertIsNone(b.move("D:\\work"))

    def test_a_new_home_with_a_space_stays_one_word(self):
        t = pathmap.Table([["/Users/x", "/home/x/My Hub"]])
        self.assertEqual(t.words("python3 /Users/x/tools/x.py --y"),
                         ('python3 "/home/x/My Hub/tools/x.py" --y', True))

    def test_lists_of_paths(self):
        t = pathmap.Table([["/Users/x", "/home/x"]])
        self.assertEqual(t.tree({"PATH": "/Users/x/bin:/usr/bin", "n": 1}, pathlists=True),
                         ({"PATH": "/home/x/bin:/usr/bin", "n": 1}, 1))


@unittest.skipIf(platforms.current.APP_OS == "mac", "the login Keychain is not the tests' to write")
class KeyStore(unittest.TestCase):
    """The OS's own store of the vault key (a file 0600 on Linux, sealed by DPAPI on Windows): a key given is kept as
    it is, never over another one, and dropped."""

    def test_value_and_drop(self):
        folder = tempfile.mkdtemp(prefix="hubexport-key-")
        self.addCleanup(platforms.current.rmtree, folder)
        key = os.urandom(32)
        self.assertIsNone(platforms.current.secret_key("main", folder))
        self.assertEqual(platforms.current.secret_key("main", folder, True, value=key), key)
        self.assertEqual(platforms.current.secret_key("main", folder), key)
        # another key there: kept, the given one is not written over it
        self.assertEqual(platforms.current.secret_key("main", folder, True, value=os.urandom(32)), key)
        with self.assertRaises(ValueError):
            platforms.current.secret_key("other", tempfile.mkdtemp(dir=folder), True, value=b"short")
        self.assertTrue(platforms.current.drop_secret_key("main", folder))
        self.assertIsNone(platforms.current.secret_key("main", folder))
        self.assertFalse(platforms.current.drop_secret_key("main", folder))


class Hubd(unittest.TestCase):
    def test_take_import(self):
        """The mark: core.imported first, then the executor without its copy stopped; the mark goes."""
        from core import daemon
        folder = tempfile.mkdtemp(prefix="hubexport-hubd-")
        self.addCleanup(platforms.current.rmtree, folder)
        with open(os.path.join(folder, hubexport.MARK), "w") as f:
            json.dump({"paths": [["/Users/x", "/home/x"]], "missing": ["maps-2", "alpha", "nobody"], "from_os": "darwin",
                       "to_os": "linux"}, f)
        hub = daemon.Hub.__new__(daemon.Hub)
        hub.state_dir, hub.state = folder, {"sessions": {"maps-2": {"state": "idle", "role": "executor"},
                                                         "alpha": {"state": "idle", "role": "executor",
                                                                   "backlog": "alpha"}}}
        calls = []
        hub.record = lambda kind, name=None, data=None, **kw: calls.append((kind, name, data))
        hub.take_import()
        self.assertEqual([(c[0], c[1]) for c in calls], [("core.imported", None), ("session.stop_requested", "maps-2")])
        self.assertEqual(calls[0][2]["paths"], [["/Users/x", "/home/x"]])
        self.assertFalse(os.path.exists(os.path.join(folder, hubexport.MARK)))


if __name__ == "__main__":
    unittest.main()
