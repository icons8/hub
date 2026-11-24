"""Default instruction budget: old behavior moves into core code, not into prompts (owner decision 2026-09-29).
Bloat is caught here, not by eye."""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Lines; this is a stretch limit against bloat, not a target (owner decision 2026-09-30). Raise it deliberately,
# with the reason in the commit.
BUDGET = {
    "hub-home/CLAUDE.md": 126,   # +2: the away item (spec d2549241); its rule comes in the core's AWAY_CARD; +4: the
    # deputies — set on his words, one form to set one up, a card to a deputy (spec d2679763)
    "plugins/hub-base/skills/hub/SKILL.md": 80,
    "plugins/hub-base/skills/voice/SKILL.md": 15,
    "plugins/hub-coordinator/skills/fleet/SKILL.md": 150,
    "plugins/hub-developer/skills/hub-developer/SKILL.md": 60,
}


class BudgetTest(unittest.TestCase):
    def test_default_instructions_stay_small(self):
        for rel, limit in BUDGET.items():
            with open(os.path.join(ROOT, rel)) as f:
                n = sum(1 for _ in f)
            self.assertLessEqual(n, limit, f"{rel}: {n} lines > {limit}")

    def test_only_four_core_plugins(self):
        # the third and fourth are available to agents from the start: Hub Developer (owner decision 2026-10-02) and
        # Agent Developer (available right after start, like hub-dev); a hub from a package has the same Hub Developer
        # (owner decision 2026-11-09: the full Hub Developer stays with everyone), not a reduced one of its own
        self.assertEqual(sorted(n for n in os.listdir(os.path.join(ROOT, "plugins")) if not n.startswith(".")),
                         ["agent-developer", "hub-base", "hub-coordinator", "hub-developer"])

if __name__ == "__main__":
    unittest.main()
