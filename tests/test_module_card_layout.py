"""Narrow module cards keep their content and actions without breaking inner scrollers."""
import tests  # noqa: F401 — isolated settings/state, never the user's hub
import json
import os
from pathlib import Path
import unittest

from tests.test_web_e2e import PageInChrome


class ModuleCardLayout(PageInChrome):
    def test_builtin_and_third_party_cards_fit_and_preserve_inner_scroll(self):
        fixture = (Path(__file__).parent / 'e2e' / 'module-cards.js').read_text()
        ready = self.page({'go': f'http://127.0.0.1:{self.port}/'},
                  {'until': '!!document.querySelector(".app")'}, {'eval': fixture},
                  {'until': '!!window.moduleCardsReady || !!window.moduleCardsError'},
                  {'eval': '[!!window.moduleCardsReady, window.moduleCardsError || null]'})[-1]['eval']
        self.assertEqual(ready, [True, None])
        shots = os.environ.get('HUB_MODULE_CARD_SHOTS') or self.state
        os.makedirs(shots, exist_ok=True)
        receipts = []
        for width in (320, 1280):
            for theme in ('light', 'dark'):
                with self.subTest(width=width, theme=theme):
                    got = self.page({'eval': f'document.documentElement.dataset.theme = "{theme}"; 1'},
                                    {'shot': os.path.join(shots, f'{width}-{theme}.png'), 'width': width,
                                     'height': 1600, 'theme': theme},
                                    {'eval': 'window.cardGeometry()'})[-1]['eval']
                    receipts.append({**got, 'theme': theme})
                    self.assertEqual(got['cards'], 2)
                    self.assertEqual(got['width'], width)
                    self.assertEqual(got['dark'], theme == 'dark')
                    self.assertEqual(got['codeWhitespace'], 'pre')
                    self.assertEqual(got['commandWhitespace'], 'pre-wrap')
                    for key in ('pageFits', 'cardsFit', 'contentsFit', 'buttonsNatural', 'codeScrolls',
                                'railScrolls', 'noCardClipping', 'desktopCap'):
                        self.assertTrue(got[key], f'{key}: {got}')
        Path(shots, 'geometry.json').write_text(json.dumps(receipts, indent=2))


def load_tests(loader, standard, pattern):
    # Reuse the browser fixture, not its full inherited journey suite.
    return unittest.TestSuite(ModuleCardLayout(n) for n in ModuleCardLayout.__dict__ if n.startswith('test_'))
