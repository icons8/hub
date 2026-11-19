"""`hub` prints what sessions, tools and pages wrote: control characters (C0 except newline and tab, DEL, C1) are
stripped - text cannot drive the terminal: change the title, move the cursor, hide lines. Both in output and in
errors (sys.exit(text) and tracebacks write to stderr)."""
import tests  # noqa: F401 - ignore the machine's settings
import os
import subprocess
import sys
import tempfile
import unittest

from core import journal

HUB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin", "hub")
EVIL = "done\x1b]0;owned\x07\x1b[2J\r\x9b31m\x00 ok"


def bad(text):
    return [c for c in text if (ord(c) < 32 and c not in "\n\t") or 0x7f <= ord(c) <= 0x9f]


class Printable(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp(prefix="printable-")
        j = journal.Journal(os.path.join(self.state, "journal.jsonl"))
        own = {"channel": "cli", "owner": True}
        j.append("session.requested", "a", {"cwd": "/w", "session_id": "s-a", "about": EVIL, "role": "agent", **own})
        j.append("session.started", "a", {"session_id": "s-a"})
        j.append("session.requested", "b", {"cwd": "/w", "session_id": "s-b", **own})
        j.append("message.received", None, {"to": "a", "text": EVIL, **own})
        j.append("session.said", "a", {"text": EVIL}, src="host")
        j.append("delegation.created", None, {"session": "b", "context": EVIL, **own})
        j.append("task.run_requested", "x", {"task": EVIL, **own})
        j.append("schedule.set", "j1", {"session": "b", "every": "1h", "prompt": EVIL, **own})
        j.append("ext.post", None, {"plugin": "p", "text": EVIL, "channel": "session", "owner": False, "sender": "a"})
        cfg = os.path.join(tempfile.mkdtemp(), "hub.json")
        with open(cfg, "w") as f:
            f.write("{}")
        self.env = {**os.environ, "HUB_STATE_DIR": self.state, "HUB_CONFIG": cfg, "HUB_CLI_WAIT": "0"}
        for k in ("HUB_SESSION", "HUB_EXTENSION", "HUB_MODULE"):
            self.env.pop(k, None)

    def hub(self, *args):
        return subprocess.run([sys.executable, HUB, *args], capture_output=True, text=True, env=self.env,
                              encoding="utf-8", timeout=30)

    def check(self, *args):
        out = self.hub(*args)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(bad(out.stdout), [], (args, out.stdout))
        self.assertIn("ok", out.stdout, args)

    def test_show(self):
        self.check("show", "a")

    def test_status(self):
        self.check("status")

    def test_listings(self):
        for args in (("task", "list"), ("task", "queue"), ("schedule", "list"), ("feed", "p")):
            self.check(*args)

    def test_errors_too(self):
        r = self.hub("send", "ghost" + EVIL.replace("\x00", ""), "hello")   # argv has no NUL
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(bad(r.stderr), [], r.stderr)
        self.assertIn("ok", r.stderr)

    def test_text_around_control_characters_stays(self):
        out = self.hub("show", "a")
        self.assertIn("done]0;owned[2J", out.stdout)
        self.assertIn("31m ok", out.stdout)

    def test_json_keeps_every_character(self):
        # scripts read --json: json.dumps(ensure_ascii=False) does not escape DEL and C1 - they cannot be dropped
        # silently, they go out escaped, and the parsed string is the same
        import json
        out = self.hub("status", "--json")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(bad(out.stdout), [])
        self.assertEqual(json.loads(out.stdout)["messages"]["m4"]["text"], EVIL)


if __name__ == "__main__":
    unittest.main()
