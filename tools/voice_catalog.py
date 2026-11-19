#!/usr/bin/env python3
"""The built-in voice's model catalog (modules/voice/models.json, spec d2126783 §4.3) from Handy's model catalog — the
catalog.json of the @handy-computer/models package (or Handy's src-tauri/src/catalog/catalog.json, the same file):

    tools/voice_catalog.py <catalog.json> [--no-funasr] [--check]

The rule, the same for every hub language: candidates are Handy's curated speech models measured for the language, under
20 % errors, a file up to 2.5 GB, up to 25 s on a laptop processor for 30 s of speech, a license we take (NOT_TAKEN —
the author's card, not Handy's field). score = errors + 0.5 * (seconds over 3). The catalog: per language the least
score (a model that is the first for another language wins a tie within 0.5), plus the one light model for many
languages (LIGHT). The rows of a language, within the catalog: the recommended — the least score (ru: GigaAM, the
owner's word); lighter — the smallest file, at most half of the recommended, under 15 %; faster — when the recommended
takes over 5 s: the fastest of the rest; more accurate — at least 10 % fewer errors; one row only — the best score of
the rest ("more"). The licenses (AUTHOR) and the observed memory peaks (MEMORY) are written here by hand: check them
on every update. --check — print, do not write."""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "modules", "voice", "models.json")
LANGS = ("ru", "en", "uk", "de", "fr", "es", "pt", "tr", "ar", "hi", "ja", "ko", "zh")   # the hub's 13, by their base
LIGHT = "whisper-small"
FIRST = {"ru": "gigaam-v3-e2e-rnnt"}   # the owner's word (spec d2109189): GigaAM for Russian
NOT_TAKEN = {"parakeet-unified-en-0.6b", "multitalker-parakeet-streaming-0.6b-v1",   # NVIDIA Open Model License
             "medasr"}   # Health AI Developer Foundations terms
FUNASR = {"SenseVoiceSmall", "Fun-ASR-MLT-Nano-2512", "Fun-ASR-Nano-2512"}   # the GGUF says FunASR Model License 1.1
NAMES = {"gigaam-v3-e2e-rnnt": "GigaAM v3", "nemotron-3.5-asr-streaming-0.6b": "Nemotron 3.5",
         "Fun-ASR-MLT-Nano-2512": "Fun-ASR-MLT-Nano", "cohere-transcribe-arabic-07-2026": "Cohere Transcribe Arabic",
         "cohere-transcribe-03-2026": "Cohere Transcribe"}
# the weights' author and license by the author's card; "file" — where the GGUF we download says otherwise
AUTHOR = {
    "gigaam-v3-e2e-rnnt": ("SaluteDevices", "MIT", "https://huggingface.co/ai-sage/GigaAM-v3"),
    "parakeet-ultra": ("moondream (on NVIDIA Parakeet TDT 0.6B v3)", "CC-BY-4.0", "https://huggingface.co/moondream/parakeet-ultra"),
    "whisper-small": ("OpenAI", "Apache-2.0", "https://huggingface.co/openai/whisper-small"),
    "Qwen3-ASR-0.6B": ("Alibaba Qwen", "Apache-2.0", "https://huggingface.co/Qwen/Qwen3-ASR-0.6B"),
    "Qwen3-ASR-1.7B": ("Alibaba Qwen", "Apache-2.0", "https://huggingface.co/Qwen/Qwen3-ASR-1.7B"),
    "nemotron-3.5-asr-streaming-0.6b": ("NVIDIA", "OpenMDW-1.1", "https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b"),
    "Fun-ASR-MLT-Nano-2512": ("Alibaba FunAudioLLM", "Apache-2.0", "https://huggingface.co/FunAudioLLM/Fun-ASR-MLT-Nano-2512"),
    "cohere-transcribe-arabic-07-2026": ("Cohere", "Apache-2.0", "https://huggingface.co/CohereLabs/cohere-transcribe-arabic-07-2026"),
    "cohere-transcribe-03-2026": ("Cohere", "Apache-2.0", "https://huggingface.co/CohereLabs/cohere-transcribe-03-2026"),
}
FILE_LICENSE = {m: "FunASR Model License 1.1" for m in FUNASR}
# families whose own language detection is not to be trusted: they get the hub's language as well (Whisper takes accented
# English for Welsh and writes nonsense — spec d2126783, live English speech: 39 % errors without, 25 % with)
LANG_HINT = {"whisper"}
# the peak memory of one recognition process seen on 25 recordings of 7-35 s (spec d2126783 §5.1), bytes; over 2 GB only
MEMORY = {"Qwen3-ASR-1.7B": 2_860_000_000, "cohere-transcribe-arabic-07-2026": 3_750_000_000}


DOC = ("The models the built-in voice downloads (modules/voice/install.py), made by tools/voice_catalog.py: list — "
       "every model pinned by its Hugging Face repository and revision, file, sha256 and size; pick — per hub language "
       "the rows of «Settings → Modules → Voice», the recommended first; vad — the speech detector for all; models — "
       "the one-model-per-language form of the first release, read by a page server older than this file.")



def candidates(cat, lang, only=None, funasr=True):
    out = []
    for m in cat["models"]:
        mid = m["id"]
        if not (m.get("curated") and m.get("role") == "asr") or mid in NOT_TAKEN or (not funasr and mid in FUNASR):
            continue
        if only is not None and mid not in only:
            continue
        res = cat.get("results", {}).get(mid, {})
        err, cpu = (res.get("accuracy", {}).get(lang) or {}).get("error"), (res.get("performance") or {}).get("cpu")
        size = m["files"][m["quant"]]["size"]
        if err is None or not cpu or err >= 20 or size > 2.5e9 or 30 / cpu > 25:
            continue
        wait = 30 / cpu
        out.append({"id": mid, "e": err, "w": wait, "size": size, "s": err + 0.5 * max(0.0, wait - 3)})
    return out


def choose(cat, funasr=True):
    """-> (catalog ids in order, {lang: [[id, role], ...]}); SystemExit names a language without an admissible model."""
    first = {}
    for lang in LANGS:
        c = candidates(cat, lang, funasr=funasr)
        if not c or (lang in FIRST and FIRST[lang] not in [x["id"] for x in c]):
            raise SystemExit(f"{lang}: no admissible model{' (' + FIRST[lang] + ')' if lang in FIRST else ''} in this catalog")
        first[lang] = next(x for x in c if x["id"] == FIRST[lang]) if lang in FIRST else min(c, key=lambda x: x["s"])
    tops = [f["id"] for f in first.values()]
    ids = []
    for lang in LANGS:
        best = first[lang]
        near = [x for x in candidates(cat, lang, funasr=funasr) if x["id"] in tops and x["s"] <= best["s"] + 0.5]
        if lang not in FIRST and near:
            best = min(near, key=lambda x: (-tops.count(x["id"]), x["s"]))
        if best["id"] not in ids:
            ids.append(best["id"])
    ids.append(LIGHT)
    pick = {}
    for lang in LANGS:
        c = candidates(cat, lang, set(ids), funasr)
        rec = next(x for x in c if x["id"] == FIRST[lang]) if lang in FIRST else min(c, key=lambda x: x["s"])
        rows, used = [[rec["id"], "recommended"]], {rec["id"]}

        def add(role, pool, key):
            pool = [x for x in pool if x["id"] not in used]
            if pool:
                x = min(pool, key=key)
                rows.append([x["id"], role])
                used.add(x["id"])
        add("lighter", [x for x in c if x["size"] <= rec["size"] / 2 and x["e"] < 15], lambda x: (x["size"], x["e"]))
        if rec["w"] > 5:
            add("faster", [x for x in c if x["w"] < rec["w"]], lambda x: x["w"])
        add("accurate", [x for x in c if x["e"] <= 0.9 * rec["e"]], lambda x: x["e"])
        if len(rows) == 1:
            add("more", c, lambda x: x["s"])
        pick[lang] = rows
    return ids, pick


def entry(cat, mid):
    m = next(x for x in cat["models"] if x["id"] == mid)
    f, res = m["files"][m["quant"]], cat.get("results", {}).get(mid, {})
    author, lic, card = AUTHOR[mid]
    out = {"name": NAMES.get(mid, m["name"]), "author": author,
           "url": f"https://huggingface.co/{m['repository']}/resolve/{m['revision']}/{f['filename']}",
           "file": f["filename"], "sha256": f["sha256"], "size": f["size"], "license": lic, "card": card,
           "languages": list(m["languages"]), "detects": bool(m["capabilities"].get("languageDetection")),
           "wait30": round(30 / res["performance"]["cpu"], 1),
           "errors": {lang: round(a["error"], 1) for lang, a in sorted(res.get("accuracy", {}).items()) if lang in LANGS}}
    if m.get("family") in LANG_HINT:
        out["lang_hint"] = True
    if mid in FILE_LICENSE:
        out["file_license"] = FILE_LICENSE[mid]
    if mid in MEMORY:
        out["memory"] = MEMORY[mid]
    return out


def build(cat, old, funasr=True):
    ids, pick = choose(cat, funasr)
    return {"_": DOC, "source": f"@handy-computer/models catalog (transcribe.cpp {cat.get('transcribeCpp')}): "
            "errors on FLEURS test at Q8_0 (per 100 words; zh, ja, ko — characters), wait30 — seconds for 30 s of "
            "speech on a Ryzen 4750U processor",
            "list": {mid: entry(cat, mid) for mid in ids}, "pick": pick, "vad": old["vad"], "models": old["models"]}


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("catalog")
    ap.add_argument("--no-funasr", action="store_true")
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    with open(a.catalog, encoding="utf-8") as f:
        cat = json.load(f)
    with open(OUT, encoding="utf-8") as f:
        old = json.load(f)
    new = build(cat, old, funasr=not a.no_funasr)
    for lang, rows in new["pick"].items():
        print(lang, " | ".join(f"{role}: {mid}" for mid, role in rows))
    if not a.check:
        with open(OUT, "w", encoding="utf-8", newline="\n") as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
            f.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
