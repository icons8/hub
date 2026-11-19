"""The language of `hub` output: a session's step (HUB_SESSION) reads English, the owner's
terminal reads the catalog of his language. A walk over the commands and their rare errors (a flag without a value, an
unknown session, a core refusal, a command left in the inbox, a module that is not up, a switched-off module): in the
pseudo-language xx every catalog string is wrapped in ⟦…⟧, so Cyrillic there is a text that bypasses the catalog; a
session gets no Cyrillic at all; the Russian owner gets the catalog's Russian."""
import tests  # noqa: F401 — do not see the machine's settings
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from core import i18n, journal, spool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HUB = os.path.join(ROOT, "bin", "hub")
CYRILLIC = re.compile("[\u0400-\u04ff]")
OWN = {"channel": "cli", "owner": True}

# (args, extra env): commands and their edges; nothing here starts a process of the hub, a browser or the network
WALK = [
    (("send", "--now", "--now"), {}),                        # a flag without a value
    (("send", "b", "\u2014"), {}),                           # nothing to send
    (("send", "b", "hello"), {}),                            # into the inbox: the core is up
    (("send", "b", "hello"), {"HUB_CLI_WAIT": "0"}),         # the core has not taken it yet
    (("decided", "x \u00b7 y"), {"REFUSE": "1"}),            # the core refused
    (("schedule", "add", "--session", "b", "--every", "soon", "--prompt", "p"), {}),
    (("schedule", "list"), {}),
    (("schedule", "remove", "nope"), {}),
    (("show", "nobody"), {}),
    (("show", "m999999"), {}),
    (("show", "--grep", "nothing-like-this"), {}),
    (("status",), {}),
    (("task", "list"), {}),
    (("task", "note", "a line"), {}),                        # a session without any task
    (("task", "show", "d999"), {}),
    (("tools", "list"), {}),
    (("tools", "request"), {}),
    (("tools", "grant", "b"), {}),
    (("wait",), {}),
    (("waiting",), {}),
    (("session", "model"), {}),
    (("session", "effort"), {}),
    (("session", "parallel", "a", "lots"), {}),
    (("session", "solo", "a"), {}),
    (("session", "rename", "a", "Bad Name!"), {}),
    (("session", "cwd", "a", "/nonexistent-folder-x"), {}),
    (("session", "fork", "a"), {}),
    (("question", "q999"), {}),
    (("answer", "nobody", "x"), {}),
    (("layout",), {}),
    (("layout", "move", "a", "no-such-group"), {}),
    (("direction", "list"), {}),
    (("direction", "board", "nope"), {}),
    (("codex", "status"), {}),
    (("codex", "use", "nobody"), {}),
    (("secret", "list"), {}),
    (("critic", "draft.md", "--task", "t"), {}),
    (("contacts", "list"), {}),
    (("coach",), {}),
    (("modules",), {}),
    (("market", "verdict", "x/y", "ok", "fine"), {}),
    (("mcp", "login"), {}),
    (("version",), {}),
    (("projects",), {}),
]


class FakeCore(threading.Thread):
    """Takes the inbox like hubd: a fact into the journal, and a refusal when the command asks for one (REFUSE)."""

    def __init__(self, state, refuse):
        super().__init__(daemon=True)
        self.state, self.refuse, self.stop = state, refuse, threading.Event()

    def run(self):
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        while not self.stop.is_set():
            for path, ev in spool.take(os.path.join(self.state, "spool")):
                seq = j.append(ev["type"], ev.get("entity"), ev.get("data"), src="cli")["seq"]
                if self.refuse.is_set():
                    j.append("core.rejected", ev.get("entity"), {"of": seq, "type": ev["type"], "reason": "not yours"})
                os.unlink(path)
            time.sleep(0.02)


class Walk(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="cli-lang-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        for n in ("a", "b"):
            j.append("session.requested", n, {"cwd": self.state, "session_id": f"s-{n}", **OWN})
            j.append("session.started", n, {"session_id": f"s-{n}"})
        self.cfg = os.path.join(tempfile.mkdtemp(), "hub.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({}, f)
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": self.cfg, "HUB_CLI_WAIT": "3",
                    "HUB_REAL_CLAUDE": os.path.join(self.state, "no-claude"), "PYTHONUTF8": "1"}
        for k in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE", "HUB_WEB_PORT"):
            self.env.pop(k, None)
        self.refuse = threading.Event()
        core = FakeCore(self.state, self.refuse)
        core.start()
        self.addCleanup(core.stop.set)

    def call(self, args, env):
        r = subprocess.run([sys.executable, HUB, *args], env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60, cwd=self.state)
        self.assertNotIn("Traceback", r.stderr, (args, r.stderr[-600:]))
        return r.stdout + r.stderr

    def run_walk(self, lang, me=None):
        """[(args, output)] of every command of the walk, without the lines of its help (`hub help <command>`):
        core/hubhelp.txt is translated by its own slice, one English file."""
        out = []
        for args, extra in WALK:
            env = {**self.env, "HUB_LANG": lang, **{k: v for k, v in extra.items() if k != "REFUSE"}}
            if me:
                env["HUB_SESSION"] = me
            (self.refuse.set if extra.get("REFUSE") else self.refuse.clear)()
            said = self.call(args, env)
            words = [w for w in args[:2] if not w.startswith("-")]
            helps = set((self.call(["help", *words], env) + self.call(["help", words[0]], env)).splitlines())
            out.append((args, "\n".join(line for line in said.splitlines() if line not in helps)))
        return out

    def test_a_session_reads_english(self):
        bad = [(a, o[:300]) for a, o in self.run_walk("ru", me="a") if CYRILLIC.search(o)]
        self.assertEqual(bad, [])

    def test_the_owner_gets_every_string_from_the_catalog(self):
        bad = [(a, o[:300]) for a, o in self.run_walk("xx") if CYRILLIC.search(o)]
        self.assertEqual(bad, [])
        # and something of it really came from the catalog (the wrapping): an English literal would bypass it
        said = [(a, o) for a, o in self.run_walk("xx") if o.strip()]
        self.assertEqual([a for a, o in said if "\u27e6" not in o], [])

    def test_the_russian_owner_reads_russian(self):
        got = dict(self.run_walk("ru"))
        self.assertIn(i18n.tr("ru", "cli.send.empty", usage="x").split(" \u2014 ")[0], got[("send", "--now", "--now")])
        self.assertTrue(CYRILLIC.search(got[("schedule", "list")]), got[("schedule", "list")])


if __name__ == "__main__":
    unittest.main()
