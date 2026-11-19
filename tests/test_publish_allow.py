"""Publish check: an allowed substring (the address of a public project containing the author's login) is not a
violation, while the same word outside it is (owner decision 2026-10-03: allow a link to a public project)."""
import tests  # noqa: F401 — ignore the machine's settings
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Allow(unittest.TestCase):
    def run_filter(self, files, words="alice|bob", allowed=None):
        """The filter reads the snapshot folder itself: files is {path: text}; allowed is its own allow file (an
        invented one by default)."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        deny, allow, snap = os.path.join(d, "deny"), os.path.join(d, "allow"), os.path.join(d, "snap")
        open(deny, "w").write(words + "\n")
        open(allow, "w").write("# comment\ngithub.com/alice-dev/tool\n")
        allow = allowed or allow
        for name, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(snap, name)), exist_ok=True)
            with open(os.path.join(snap, name), "w", encoding="utf-8") as f:
                f.write(text)
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "publish_allow.py"), deny, "-i", allow, snap],
                           capture_output=True, text=True, check=True)
        return r.stdout.splitlines()

    def test_the_private_modules_repo_is_allowed_other_uses_of_its_owner_are_not(self):
        """Owner decision 2026-10-26: allow the repo address of the private modules and the official catalog, only
        that, as an exact substring (the owner's login is in the deny list; it is not in this test's text, and is taken
        from the allow entry itself)."""
        real = os.path.join(ROOT, "tools", "publish-allow.txt")
        spec = importlib.util.spec_from_file_location("publish_allow", os.path.join(ROOT, "tools", "publish_allow.py"))
        publish_allow = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(publish_allow)
        from core import marketplaces   # the official catalog lives in that repository: its address is the entry
        repo = [x for x in publish_allow.allowed(real) if x == marketplaces.SOURCE]
        from core import embed
        if not repo and not embed.file("modules/private.json", ROOT):   # the core's own or an embed's
            return   # the open tree: its allow file has no line for what only the cut code names (layer.cut_open)
        self.assertEqual(len(repo), 1, repo)
        login = repo[0].split("/")[0]
        out = self.run_filter({"a.py": f'SOURCE = "{repo[0]}"\n', "b.py": f'URL = "https://github.com/{repo[0]}.git"\n',
                               "c.py": f'OTHER = "{login}/agents-hub-x"\n', "d.md": f"author — {login}\n"},
                              words=login, allowed=real)
        self.assertEqual([x.split(":")[0] for x in out], ["c.py", "d.md"])

    def test_the_credits_pass_only_in_readme_and_contributors_and_only_in_their_form(self):
        """Owner decision 2026-11-17: the author line and the contributors' profile links of README.md and
        CONTRIBUTORS.md are published although the name and the login are forbidden words elsewhere."""
        line = "Created by Alice Smith ([@alice](https://github.com/alice))"
        link = "- [@bob](https://github.com/bob): 3 pull requests"
        out = self.run_filter({"README.md": f"# Hub\n\n{line}\n\n{link}\n",
                               "CONTRIBUTORS.md": f"{line}\n{link}\n",
                               "notes.md": f"{line}\n{link}\n",
                               "docs.md": "Alice Smith wrote it\n",
                               "sub/README.md": f"{line}\n"},
                              words="alice|smith|bob")
        self.assertEqual(out, ["docs.md:1:Alice Smith wrote it", f"notes.md:1:{line}", f"notes.md:2:{link}",
                               f"sub/README.md:1:{line}"])
        # in those two files, the name outside the author line, a word after it, and a link to another address still count
        out = self.run_filter({"README.md": f"{line} and bob\nAlice Smith\n[@bob](https://github.com/bob/private)\n"
                                            f"Created by Alice Smith ([@alice](https://github.com/bob))\n"},
                              words="alice|smith|bob")
        self.assertEqual([x.split(":")[1] for x in out], ["1", "2", "3", "4"])

    def test_allowed_url_passes_name_elsewhere_does_not(self):
        out = self.run_filter({"a.js": "see https://github.com/alice-dev/tool\n",
                               "b.md": "x\nthanks Alice for the help\n",
                               "c.md": "https://github.com/alice-dev/tool and Bob\n"})
        self.assertEqual(out, ["b.md:2:thanks Alice for the help", "c.md:1:https://github.com/alice-dev/tool and Bob"])


if __name__ == "__main__":
    unittest.main()
