"""After installing on a Mac, `hub` was "command not found": the installer puts the hub and hub-telegram wrappers
in ~/.local/bin (with the same Python as the hub) and, if that folder is not in PATH, appends it to ~/.zshrc once."""
import tests  # noqa: F401 — keep the machine's settings out of the test
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from tests import winsafe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINK = os.path.join(ROOT, "deploy", "mac", "link-commands.sh")


@unittest.skipIf(os.name == "nt", "the macOS and Linux installer is an sh script")
class LinkCommands(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="link-")
        self.addCleanup(shutil.rmtree, self.home, True)

    def run_link(self, path="/usr/bin:/bin", shell="/bin/zsh", out="stdout"):
        env = {"HOME": self.home, "PATH": path, "SHELL": shell}
        r = subprocess.run(["/bin/sh", LINK, ROOT, sys.executable], env=env, capture_output=True, text=True, check=True)
        return getattr(r, out)

    def put(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_hub_works_from_a_new_terminal(self):
        out = self.run_link()
        self.assertIn("new terminal window", out)
        hub = os.path.join(self.home, ".local", "bin", "hub")
        r = subprocess.run([hub, "--help"], capture_output=True, text=True,
                           env={"HOME": self.home, "PATH": "/usr/bin:/bin", "HUB_STATE_DIR": self.home})
        self.assertIn("session new", r.stdout + r.stderr)          # runs with the chosen Python
        self.run_link()                                                 # second time — PATH is not appended again
        with open(os.path.join(self.home, ".zshrc")) as f:
            self.assertEqual(f.read().count("hub-core: ~/.local/bin"), 1)

    def test_path_already_has_the_folder(self):
        self.run_link(f"{self.home}/.local/bin:/usr/bin:/bin")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".zshrc")))

    def test_a_link_to_another_program_is_not_written_through(self):
        # ~/.local/bin/hub is a link to someone else's program (GitHub's hub, a dotfiles repo): the write went through
        # the link and replaced that program's file with the hub's wrapper (tests research 03.11, S1)
        foreign = os.path.join(self.home, "foreign", "hub")
        self.put(foreign, "#!/bin/sh\necho FOREIGN\n")
        link = os.path.join(self.home, ".local", "bin", "hub")
        os.makedirs(os.path.dirname(link))
        winsafe.symlink(self, foreign, link)
        err = self.run_link(f"{self.home}/.local/bin:/usr/bin:/bin", out="stderr")
        self.assertEqual(self.read(foreign), "#!/bin/sh\necho FOREIGN\n")
        self.assertEqual(os.readlink(link), foreign)                     # the person's link stays
        self.assertIn(link, err)                                         # and they are told why hub is not the hub's
        self.assertIn("/bin/hub-telegram", self.read(os.path.join(self.home, ".local", "bin", "hub-telegram")))

    def test_someone_elses_hub_is_left_as_it_is(self):
        hub = os.path.join(self.home, ".local", "bin", "hub")
        self.put(hub, "#!/bin/sh\necho GITHUB-HUB-CLI\n")
        self.run_link(f"{self.home}/.local/bin:/usr/bin:/bin")
        self.assertEqual(self.read(hub), "#!/bin/sh\necho GITHUB-HUB-CLI\n")

    def test_a_link_to_a_hubs_own_bin_is_replaced_not_written_through(self):
        # the Linux install before 06.10 linked ~/.local/bin/hub to the code's bin/hub: writing through the link would
        # turn the hub's own program into a wrapper that runs itself
        code = os.path.join(self.home, "code", "bin")
        self.put(os.path.join(code, "hub"), "THE HUB ITSELF\n")
        self.put(os.path.join(code, "hub-up"), "\n")
        link = os.path.join(self.home, ".local", "bin", "hub")
        os.makedirs(os.path.dirname(link))
        winsafe.symlink(self, os.path.join(code, "hub"), link)
        self.run_link(f"{self.home}/.local/bin:/usr/bin:/bin")
        self.assertEqual(self.read(os.path.join(code, "hub")), "THE HUB ITSELF\n")
        self.assertFalse(os.path.islink(link))
        self.assertIn(f'"{ROOT}/bin/hub"', self.read(link))

    def test_the_wrapper_of_an_earlier_copy_is_replaced(self):
        hub = os.path.join(self.home, ".local", "bin", "hub")
        self.put(hub, '#!/bin/sh\nexec "/usr/bin/python3" "/old/copy/bin/hub" "$@"\n')
        self.run_link(f"{self.home}/.local/bin:/usr/bin:/bin")
        self.assertIn(f'"{ROOT}/bin/hub"', self.read(hub))

    def test_bash_with_only_a_profile_keeps_reading_it(self):
        # a login bash reads only the first of ~/.bash_profile, ~/.bash_login, ~/.profile: a new ~/.bash_profile made
        # ~/.profile (its PATH and variables) unread (tests research 03.11, S3)
        self.put(os.path.join(self.home, ".profile"), "export MINE=1\n")
        self.run_link(shell="/bin/bash")
        self.assertFalse(os.path.exists(os.path.join(self.home, ".bash_profile")))
        profile = self.read(os.path.join(self.home, ".profile"))
        self.assertTrue(profile.startswith("export MINE=1\n"))
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', profile)

    def test_bash_without_login_files_gets_a_bash_profile(self):
        self.run_link(shell="/bin/bash")
        self.assertIn("hub-core: ~/.local/bin", self.read(os.path.join(self.home, ".bash_profile")))


if __name__ == "__main__":
    unittest.main()
