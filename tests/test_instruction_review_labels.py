"""The review confirmation describes edits to agent instructions, not model training."""
import tests  # noqa: F401
import json
import os
from unittest import mock

from core import i18n
from tests.test_web_e2e import PageInChrome


def stem(lang):
    """The word for "instructions" of a language, as its catalog says it: the longest word of the button, cut to 8."""
    return max(i18n.tr(lang, "web.dialogs.train_ok").split(), key=len)[:8].lower()


class InstructionLabels(PageInChrome):
    def test_menu_and_confirmation_name_the_instruction_action_in_both_complete_languages(self):
        config = os.path.join(self.state, "language.json")
        with mock.patch.dict(os.environ, {"HUB_CONFIG": config}):
            for lang, word in (("en", stem("en")), ("ru", stem("ru"))):
                with open(config, "w", encoding="utf-8") as f:
                    json.dump({"language": lang}, f)
                got = self.page({"go": f"http://127.0.0.1:{self.port}/"},
                                {"eval": "import('/static/js/dialogs.js').then(m => m.askTrain('sample-agent')); 1"},
                                {"until": "document.getElementById('confirm-dlg').open"},
                                {"eval": "['confirm-title','confirm-text','confirm-ok'].map(id => document.getElementById(id).textContent)"},
                                {"eval": "import('/static/js/i18n.js').then(m => window.instructionMenu = [m.t('web.main.train'),m.t('web.main.train_hint')]); 1"},
                                {"until": "!!window.instructionMenu"},
                                {"eval": "window.instructionMenu"})
                for text in got[3]["eval"] + got[6]["eval"]:
                    self.assertIn(word, text.lower())
                self.assertIn("sample-agent", got[3]["eval"][1])
                self.assertFalse(any(e["type"] == "message.received" for e in self.spooled()))


def load_tests(loader, standard, pattern):
    import unittest
    return unittest.TestSuite(InstructionLabels(n) for n in InstructionLabels.__dict__ if n.startswith("test_"))
