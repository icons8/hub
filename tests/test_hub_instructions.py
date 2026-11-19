"""Hub instructions about directions (owner requirement: directions should appear on their own as work goes on, either
when it becomes clear that something is a direction, or when the owner asks for one; a report from another hub,
2026-10-03: the order "agent → direction → executor" lived only in the fleet skill — without it the hub started an
executor where a direction was needed)."""
import glob
import os
import re
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*p):
    with open(os.path.join(REPO, *p), encoding="utf-8") as f:
        return f.read()


class Directions(unittest.TestCase):
    def test_hub_claude_md_task_step_knows_directions(self):
        text = read("hub-home", "CLAUDE.md")
        task = re.search(r"\n2\. \*\*Task\*\*.*?(?=\n3\. )", text, re.S).group(0)
        for must in ("hub direction list", "hub direction new", "hub decided", "direction"):
            self.assertIn(must, task)

    def test_hub_claude_md_teaches_fyi(self):
        # eval 2026-10-17: the line about `--fyi` lived only in the skills, the hub does not open them for such a turn
        # and never chose the flag (0 of 6), while CLAUDE.md taught "info for later — no flag"; with the line — 6 of 6
        text = read("hub-home", "CLAUDE.md")
        self.assertIn('hub send <name> --fyi "<text>"', re.sub(r"\s+", " ", text))
        self.assertNotIn("info for later — no flag", re.sub(r"\s+", " ", text))

    def test_fleet_skill_is_found_for_my_direction(self):
        head = read("plugins", "hub-coordinator", "skills", "fleet", "SKILL.md").split("---")[1]
        self.assertIn("direction", head.lower())



class Skills(unittest.TestCase):
    """Skills teach commands the core will accept: only the hub restarts the whole hub (core/restart.py), no forks."""

    def skills(self):
        return glob.glob(os.path.join(REPO, "plugins", "**", "SKILL.md"), recursive=True) + \
            glob.glob(os.path.join(REPO, "modules", "*", "plugin", "**", "SKILL.md"), recursive=True)

    def test_only_the_hub_skill_restarts_the_whole_hub(self):
        # the core accepts a restart of the whole hub only from the owner and the hub: another session's skill asks
        # the hub, otherwise its command is rejected every time
        for path in self.skills():
            if os.sep + "hub-coordinator" + os.sep in path:
                continue
            with open(path, encoding="utf-8") as f:
                self.assertEqual(re.findall(r'hub restart "[^"]*"', f.read()), [], path)

    def test_skills_never_restart_a_part_by_mistake(self):
        from core import restart
        for path in self.skills():
            with open(path, encoding="utf-8") as f:
                for why in re.findall(r'hub restart "([^"]*)"', f.read()):
                    self.assertNotIn(why, restart.PARTS + ("hubd",), path)   # a bare part name would restart it

    def test_the_update_module_asks_hub_developer_to_ask_the_hub(self):
        # the update module tells Hub Developer what to do after a conflict: the core rejects a whole-hub restart from it
        from core import restart
        text = read("modules", "update", "update.py")
        parts = "|".join(restart.PARTS + ("ext:", "mod:", "--cancel"))
        # any "hub restart" except parts and a mention in code (`hub restart` in backticks inside a docstring)
        self.assertEqual(re.findall(rf"hub restart(?!`| (?:{parts}))", text), [])
        self.assertIn("restart the whole hub", text)

    def test_no_forks_and_no_old_hub_name(self):
        for path in self.skills():
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotIn("hub session fork", text, path)
            self.assertNotIn("hub send hub-core", text, path)


if __name__ == "__main__":
    unittest.main()
