"""The page mirrors for a right-to-left language (Arabic, slice L11) because its styles say "the line start" and "the
line end", not "left" and "right": margin-inline-start, inset-inline-end, text-align: start. A new physical property
would stay on the left in an Arabic page; one that must stay physical (horizontal centering, a drawn glyph, an off-screen
helper) says so on its line: `/* physical: <why> */`, or is a [dir="rtl"] rule itself."""
import tests  # noqa: F401 — keep the machine's settings out
import glob
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("modules/web/static/css/*.css", "modules/web/static/clean/*.css", "modules/web/bundled/*/*.css", "modules/files/static/*.css")
PHYSICAL = re.compile(r"(?:(?<=[{;\s])|^)(?:(?:margin|padding|border)-(?:left|right)(?:-\w+)?|left|right|"
                      r"border-(?:top|bottom)-(?:left|right)-radius)\s*:(?!:)|text-align:\s*(?:left|right)\b|"
                      r"float:\s*(?:left|right)\b")


def physical(text):
    """(line number, line) of the physical left/right declarations outside comments, not marked and not RTL rules."""
    out = []
    for no, line in enumerate(text.splitlines(), 1):
        if "physical" in line or '[dir="rtl"]' in line or "translateX(-50%)" in line:
            continue
        code = re.sub(r"/\*.*?\*/", " ", line)
        if PHYSICAL.search(code):
            out.append((no, line.strip()))
    return out


class LogicalProperties(unittest.TestCase):
    def test_the_styles_say_line_start_and_end_not_left_and_right(self):
        bad = {}
        for pattern in FILES:
            for path in sorted(glob.glob(os.path.join(ROOT, pattern))):
                with open(path, encoding="utf-8") as f:
                    found = physical(f.read())
                if found:
                    bad[os.path.relpath(path, ROOT)] = found
        self.maxDiff = None
        self.assertEqual(bad, {})

    def test_the_check_sees_what_it_should(self):
        self.assertEqual([n for n, _ in physical(".a { margin-left: 4px; }\n.b { margin-inline-start: 4px; }\n"
                                                 ".c { left: 0; }\n.d::before { content: 'x'; }\n.e:hover { color: red }\n"
                                                 ".f { text-align: right; }\n.g { left: 50%; transform: translateX(-50%); }\n"
                                                 ".h { right: 0; }   /* physical: on purpose */\n"
                                                 "/* left: a comment */\n.i.left:hover { top: 0 }\n")],
                         [1, 3, 6])


if __name__ == "__main__":
    unittest.main()
