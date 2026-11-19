"""What sessions showed the owner: the page serves these images over the tailnet too (PR #116, core/shown.py).
Review 13.10: a path at the end of a sentence with a period, file:// with %20, a name with spaces in a step path, two
paths on adjacent lines."""
import tests  # noqa: F401 — ignore the machine's settings
import unittest

from core import shown as shown_mod


class Shown(unittest.TestCase):
    def feed(self, *events):
        shown = {}
        for kind, data in events:
            shown_mod.note(shown, kind, data)
        return shown

    def test_paths_are_taken_as_the_page_will_ask_for_them(self):
        shown = self.feed(
            ("session.requested", {"cwd": "/w", "session_id": "s-a"}),
            ("session.said", {"text": "Done, screenshot: file:///tmp/shots/page.png. And also file:///tmp/my%20shots/b.png"}),
            ("turn.tool", {"id": "t1", "name": "Read", "input": {"file_path": "/Users/x/Desktop/Screenshot 1.png"}}),
            ("turn.tool", {"id": "t2", "name": "Bash", "input": {"command": "ls /tmp/a.png\nls /tmp/b.jpg"}}))
        for p in ("/tmp/shots/page.png", "/tmp/my shots/b.png", "/Users/x/Desktop/Screenshot 1.png", "/tmp/a.png",
                  "/tmp/b.jpg"):
            self.assertIn(p, shown, p)
        self.assertFalse([p for p in shown if "\\n" in p or "\n" in p])


if __name__ == "__main__":
    unittest.main()
