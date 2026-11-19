"""tools/voice_catalog.py — the rule that makes the voice's model catalog from Handy's (spec d2126783 §4.3), on a small
made-up catalog: the recommended row by the score, one named exception (ru), the licenses not taken, the limits of
size and wait, the light model, the roles lighter / faster / more accurate / more."""
import tests  # noqa: F401 — ignore the machine's settings
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import voice_catalog  # noqa: E402


def model(mid, langs, errors, wait, size, family="x", detects=True):
    return ({"id": mid, "name": mid, "role": "asr", "curated": True, "family": family, "languages": langs,
             "capabilities": {"languageDetection": detects}, "quant": "Q8_0", "repository": f"handy-computer/{mid}-gguf",
             "revision": "a" * 40, "files": {"Q8_0": {"filename": f"{mid}-Q8_0.gguf", "size": size, "sha256": "b" * 64}}},
            {"accuracy": {lang: {"error": e} for lang, e in errors.items()}, "performance": {"cpu": 30 / wait}})


CATALOG = [
    model("gigaam-v3-e2e-rnnt", ["ru"], {"ru": 5.4}, 3.8, 274_000_000, detects=False),
    model("parakeet-ultra", ["ru", "en"], {"ru": 5.6, "en": 4.3}, 2.3, 740_000_000),
    model("parakeet-unified-en-0.6b", ["en"], {"en": 4.0}, 2.1, 731_000_000),   # its license is not taken
    model("whisper-small", ["ru", "en", "tr"], {"ru": 11.9, "en": 6.5, "tr": 16.0}, 5.1, 270_000_000, family="whisper"),
    model("Qwen3-ASR-1.7B", ["ru", "en", "tr"], {"ru": 6.2, "en": 3.2, "tr": 9.5}, 12.1, 2_185_000_000),
    model("nemotron", ["en", "tr"], {"en": 7.9, "tr": 15.4}, 2.8, 751_000_000),
    model("too-big", ["en"], {"en": 2.0}, 2.0, 3_000_000_000),
    model("too-slow", ["en"], {"en": 2.5}, 30.0, 500_000_000),
]


class Rule(unittest.TestCase):
    def setUp(self):
        self.cat = {"models": [m for m, _ in CATALOG], "results": {m["id"]: r for m, r in CATALOG}}
        p = mock.patch.object(voice_catalog, "LANGS", ("ru", "en", "tr"))
        p.start()
        self.addCleanup(p.stop)

    def test_the_catalog_and_the_rows(self):
        ids, pick = voice_catalog.choose(self.cat)
        # ru — GigaAM by the owner's word; en — Parakeet Ultra (Unified English: license; too big, too slow: limits);
        # tr — Qwen3-ASR 1.7B (9.5 + 0.5 * 9.1 beats Nemotron's 15.4); plus the light one
        self.assertEqual(ids, ["gigaam-v3-e2e-rnnt", "parakeet-ultra", "Qwen3-ASR-1.7B", "whisper-small"])
        self.assertEqual(pick["ru"], [["gigaam-v3-e2e-rnnt", "recommended"], ["parakeet-ultra", "more"]])
        self.assertEqual(pick["en"], [["parakeet-ultra", "recommended"], ["whisper-small", "lighter"],
                                      ["Qwen3-ASR-1.7B", "accurate"]])
        self.assertEqual(pick["tr"], [["Qwen3-ASR-1.7B", "recommended"], ["whisper-small", "faster"]])

    def test_an_entry_pins_the_file_by_its_revision(self):
        e = voice_catalog.entry(self.cat, "Qwen3-ASR-1.7B")
        self.assertEqual(e["url"], "https://huggingface.co/handy-computer/Qwen3-ASR-1.7B-gguf/resolve/" + "a" * 40 +
                         "/Qwen3-ASR-1.7B-Q8_0.gguf")
        self.assertEqual((e["sha256"], e["size"], e["license"], e["wait30"]), ("b" * 64, 2_185_000_000, "Apache-2.0", 12.1))
        self.assertEqual(e["memory"], voice_catalog.MEMORY["Qwen3-ASR-1.7B"])
        self.assertNotIn("lang_hint", e)
        self.assertTrue(voice_catalog.entry(self.cat, "whisper-small")["lang_hint"])


if __name__ == "__main__":
    unittest.main()
