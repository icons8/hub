#!/usr/bin/env python3
"""Translate the catalogs with the model (i18n/README.md "Translating"): what a language lacks or has stale — in batches
through `claude -p`, checked by script, written and stamped.

    tools/translate.py [--all | <language> …]       translate what is missing or stale (tools/i18n.py status)
    tools/translate.py glossary [--all | <language> …]  the glossary terms (glossary.* keys) first, from the language's
                                                      own earlier translations
    tools/translate.py review [--all | <language> …]  a second model reads every translation; what it flags is
                                                      translated again with its note (--no-fix — only the report)

    --keys K1,K2        only these keys          --jobs N    batches at once (4)
    --budget USD        stop starting batches when the spend of this run reaches it
    --ledger FILE       append every call (language, model, keys, USD) as a JSON line
    --model M           the translating model (claude-opus-5-5); the reviewer — --review-model (claude-sonnet-5-5)
    --catalogs DIR      an embed's catalogs (embed/<name>/i18n, core/embed.py) instead of the core's i18n/: its own
                        English, sync.json and translations; the glossary, its examples and the languages' stage stay
                        the core's
    --sample N          review: N keys per language, not all — half of them short page texts (buttons, labels), half
                        any, the same for the same N
    --dry               show the batches and the prompt, call nothing

Every answer is checked before it is written: the same {placeholders}, the plural forms of the language (CLDR), the
same flags, hub commands, paths, file names, URLs and `code`, letters of the language's own script. A failing key is
asked again with the reason; failing twice it stays missing (the page shows English) and is listed at the end.
The release refuses a ready language that is not whole and fresh (tools/i18n.py check --release) — run this first.
"""
import argparse
import concurrent.futures
import json
import os
import re
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "tools"))
from core import i18n  # noqa: E402

import importlib.util  # noqa: E402
_spec = importlib.util.spec_from_file_location("i18n_tool", os.path.join(REPO, "tools", "i18n.py"))
tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tool)

MODEL, REVIEW_MODEL = "claude-opus-5-5", "claude-sonnet-5-5"
GLOSSARY = "glossary."                         # catalog keys of the terms: every language has its own column
CATALOGS = tool.CATALOGS                       # the catalogs this run reads and writes (tests: a copy)
TERMS = None   # the catalogs of the glossary and the languages' stage: an embed's run (--catalogs) — the core's; None — CATALOGS
BATCH_CHARS, BATCH_KEYS = 12000, 100           # one call: the batch as it is sent (English, short Russian)
REVIEW_CHARS, REVIEW_KEYS = 14000, 120
RU_UPTO = 80                                   # the Russian goes along only for short texts: there it settles the meaning
TIMEOUT = 600
TRIES = 3

ABOUT = (
    "Hub by Icons8 is a tool for one person, the owner, who runs a fleet of AI coding sessions (Claude Code, Codex) "
    "through a web page, Telegram and a command line (`hub`). The interface strings are short UI texts: buttons, "
    "labels, hints, status lines, error messages, command-line output and help.")

RULES = """Rules:
- Translate the meaning for a native speaker of {name}, in the register of good software UI: plain, short, natural; not word for word. Keep the length close to the English one; a button or label (a short key such as *.btn, *_title, *.tab, a text of 1-3 words) stays as short as the English.
- Use the glossary terms below for these concepts, every time, with the grammar the sentence needs.
- Keep verbatim, untranslated: every {{placeholder}} exactly as written (the name inside the braces never changes; {{{{ and }}}} are literal braces), command lines and their parts (hub session new <name>, hub restart, hub bus invite …), flags (--work, -n), file names and paths (hub.json, CLAUDE.md, ~/.config/…), URLs, environment variables, JSON keys and values in code style, text inside `backticks`, keyboard keys (Ctrl+C, ⌘R), emoji, arrows and symbols (→ · • ✓), product names (Hub by Icons8 when used as a name, Claude, Claude Code, Codex, Telegram, Teams, GitHub, Chrome, MCP, Windows, macOS, Linux).
- Placeholders stand for values: {{name}}, {{session}} — a name; {{count}}, {{n}} — a number; {{when}}, {{ago}} — a time; {{why}}, {{reason}}, {{error}} — a phrase. Build the sentence so the value fits in it without changing its form.
- Keep the punctuation that is part of the UI: a trailing colon, an ellipsis, a leading "!" or "·", line breaks (\\n), Markdown (**bold**, lists) and HTML tags as they are. Quotes «…» or "…" around a name — the usual quotes of {name}.
- The owner is a person of any gender: use the language's plain word for the owner and phrase sentences so they need no gender where the language allows; no gender-star, colon or slash forms; never "master".
- An English text with plural forms comes as an object (one/other): answer with "forms" holding exactly these {name} categories: {forms}. CLDR samples of each: {samples}. Each form is a whole phrase with the number inside it — never cut the phrase into pieces.
- A plain text comes as a string: answer with "text".
- The register follows the Russian: where it addresses the owner with the polite form, use the polite address of {name}; where it addresses a session with the familiar form (the core's words to sessions), the familiar one.
- The Russian text (ru) is the same string by the same author: use it to understand the meaning when the English is short or ambiguous; translate from the English.
- A glossary.* key is a term of the glossary: answer with the term in its base form (the part of speech of the English), the word the language's software uses for this concept; the meaning in "note" decides. "examples" are earlier draft translations of this interface: keep their word if it is right for the note, choose a better one if not (the drafts are fixed afterwards).
- Answer for every input item, with its number i (not the key)."""

SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["i"], "properties": {
        "i": {"type": "integer"}, "text": {"type": "string"},
        "forms": {"type": "object", "additionalProperties": {"type": "string"}}}}}}}

REVIEW_SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["k", "problem"], "properties": {
        "k": {"type": "string"}, "problem": {"type": "string"}}}}}}

REVIEW_RULES = """You review the {name} ({lang}) translation of user interface strings of Hub by Icons8. {about}
Each input item: k (the key), en (the source), ru (the same string in Russian, for the meaning), tr (the {name} translation).
Report ONLY real defects a native speaker or a user would notice:
- the meaning differs from the English (wrong, missing or added information, a wrong sense of an ambiguous word);
- a concept is not translated with its glossary term (below);
- English left untranslated where it is not a name, a command, a flag, a path or code;
- broken grammar, a wrong plural form, an unnatural or machine-like phrase;
- a button or label clearly longer than needed.
Do not report style preferences, synonyms of equal quality, kept command lines, flags, paths, placeholders or product names. The register follows the Russian: polite where it addresses the owner, familiar where the core addresses a session — that is not a defect.
For each defect: k and problem — one line in English: what is wrong and what it should be. No defect — leave the item out; an empty list is a fine answer."""


# --- the batches and their prompt ------------------------------------------------------------------------------


def catalogs():
    return tool._catalog(CATALOGS, i18n.SOURCE) or {}, tool._catalog(CATALOGS, "ru") or {}


def samples(lang):
    """The CLDR samples of each plural category of the language: the numbers the core picks it for."""
    out = {}
    for n in list(range(0, 112)) + [1000000, 1.5]:
        out.setdefault(i18n.plural(lang, n), []).append(n)
    return "; ".join(f"{k}: {', '.join(str(x) for x in v[:6])}" for k, v in sorted(out.items()))


def glossary_lines(lang, fresh=False):
    """The glossary terms (glossary.* keys) and how the language renders them: the core's catalogs, an embed's too."""
    en = tool._catalog(TERMS or CATALOGS, i18n.SOURCE) or {}
    cat = {} if fresh else tool._catalog(TERMS or CATALOGS, lang) or {}
    notes = glossary_notes()
    rows = []
    for key in sorted(k for k in en if k.startswith(GLOSSARY)):
        mine = cat.get(key)
        rows.append(f"- {en[key]} → {mine if isinstance(mine, str) else '(choose one and keep it)'}"
                    + (f" — {notes[key]}" if notes.get(key) else ""))
    return "\n".join(rows)


def glossary_notes():
    return tool._read(os.path.join(TERMS or CATALOGS, tool.NOTES), {}).get("terms", {})   # English only


def examples(lang, term, most=5):
    """Earlier translations of the language's catalog whose English has the term: how this interface renders it (the
    core's: an embed's catalogs are few and take its terms)."""
    en = tool._catalog(TERMS or CATALOGS, i18n.SOURCE) or {}
    cat = tool._catalog(TERMS or CATALOGS, lang) or {}
    word = re.compile(rf"\b{re.escape(term)}s?\b", re.I)
    out = [{"en": en[k], "tr": cat[k]} for k in sorted(cat) if not k.startswith(GLOSSARY)
           and isinstance(en.get(k), str) and isinstance(cat[k], str) and len(en[k]) < 160 and word.search(en[k])]
    return out[:most]


def prompt(lang, fresh=False):
    name = i18n.NAMES.get(lang, lang)
    need, may = i18n.forms(lang)
    forms = ", ".join(sorted(need)) + (f" (and optionally {', '.join(sorted(may - need))})" if may - need else "")
    return (f"You translate the user interface of Hub by Icons8 from English into {name} ({lang}). {ABOUT}\n\n"
            + RULES.format(name=name, forms=forms, samples=samples(lang))
            + f"\n\nGlossary (English → {name} — what it means):\n" + glossary_lines(lang, fresh))


def batches(items, chars, keys):
    """Consecutive keys (one area stays together) up to the size of one call."""
    out, cur, size = [], [], 0
    for item in items:
        n = len(json.dumps(item, ensure_ascii=False))
        if cur and (size + n > chars or len(cur) >= keys):
            out.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += n
    return out + ([cur] if cur else [])


# --- the checks ---------------------------------------------------------------------------------------------------

_FLAG = re.compile(r"(?<![\w-])--?[a-zA-Z][\w-]*")
_PATH = re.compile(r"(?<![\w/.])(?:~/[\w.@~-]+(?:/[\w.@~-]+)*|/[\w.@~-]+(?:/[\w.@~-]+)+)/?|\b[\w-]+\.(?:json|jsonl|md|py|sh|ps1|log|toml|txt|js|css|html|mjs|deb|app|exe|cmd|env)\b")
_URL = re.compile(r"https?://\S+?(?=[).,;:!?»”\"']*(?:\s|$))")
_CODE = re.compile(r"`[^`\n]+`")
_MARKS = re.compile("[\u200b\u200e\u200f\u202a-\u202e\u2066-\u2069\ufeff]")   # joiners (200c, 200d) are letters' business
_TAG = re.compile(r"</?(?:a|b|i|u|s|br|code|pre|kbd|em|strong|small|span|div|p|ul|ol|li|sup|sub)\b[^>]*>")   # not <name>


def _hub_commands():
    try:
        with open(os.path.join(REPO, "core", "hubhelp.txt"), encoding="utf-8") as f:
            return {m.group(1) for m in re.finditer(r"^hub ([a-z][a-z-]*)", f.read(), re.M)}
    except OSError:
        return set()


COMMANDS = _hub_commands()
# a command, not "a hub restart" or "the hub on their word" in prose
_HUB = re.compile(r"(?<!\ba )(?<!\bthe )(?<!\byour )(?<!\bits )(?<!\bthis )(?<!\bevery )(?<!\bno )(?<!\bnew )\bhub ([a-z][a-z-]*)")
PROSE = {"version", "journal", "on", "off", "page"}   # "the hub version", "a hub journal": words, not commands


def loose(text):
    """`code` as it is compared: a fence is not a span, and a <metavariable> inside may be translated."""
    return re.sub(r"<[^<>`\n]*>", "<>", text.replace("```", ""))


def kept(text):
    """What a translation must carry over unchanged: flags, hub commands, paths, URLs, `code`, tags, literal braces."""
    found = set(_FLAG.findall(text)) | set(_URL.findall(text)) | set(_CODE.findall(loose(text))) | set(_TAG.findall(text))
    found |= {m.group(0) for m in _PATH.finditer(text) if not _URL.search(text[max(0, m.start() - 8):m.end()])}
    found |= {"hub " + m.group(1) for m in _HUB.finditer(text) if m.group(1) in COMMANDS - PROSE}
    return found


def check(lang, source, got):
    """Why the translation of one key is not acceptable; None — it is."""
    need, may = i18n.forms(lang)
    if isinstance(source, dict):
        if not isinstance(got, dict) or not got:
            return f"plural forms are needed: an object with {sorted(need)}"
        if not need <= set(got) <= may:
            return f"plural forms {sorted(got)}, {lang} needs exactly {sorted(need)}" + \
                (f" (and may have {sorted(may - need)})" if may - need else "")
        if not all(isinstance(v, str) and v.strip() for v in got.values()):
            return "every form is a non-empty text"
        texts, sources = list(got.values()), list(source.values())
    else:
        if not isinstance(got, str) or (source.strip() and not got.strip()):
            return "a text is needed"
        texts, sources = [got], [source]
    if i18n.placeholders(got) != i18n.placeholders(source):
        return f"placeholders {sorted(i18n.placeholders(got))}, the English has {sorted(i18n.placeholders(source))}"
    for t in texts:
        extra = i18n.placeholders(t) - i18n.placeholders(source)
        if extra:
            return f"unknown placeholders {sorted(extra)}"
    if sum(t.count("{{") for t in texts) * len(sources) != sum(s.count("{{") for s in sources) * len(texts):
        return "literal braces {{ }} as in the English"
    want = set().union(*(kept(s) for s in sources))
    lost = sorted(x for x in want if not any(x in t or x in loose(t) for t in texts))
    if lost:
        return f"keep verbatim: {', '.join(lost)}"
    if any(_MARKS.search(t) for t in texts):
        return "no invisible direction marks or zero-width spaces: the page isolates the values itself"
    for t in texts:
        bad = tool.foreign_script(t, lang)
        if bad:
            return f"letters of another script ({', '.join(bad)})"
    return None


# --- the model ------------------------------------------------------------------------------------------------------


class Spend:
    def __init__(self, budget=None, ledger=None):
        self.usd, self.budget, self.ledger = 0.0, budget, ledger

    def add(self, lang, model, keys, usd):
        self.usd += usd or 0
        if self.ledger:
            os.makedirs(os.path.dirname(os.path.abspath(self.ledger)), exist_ok=True)
            with open(self.ledger, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": int(time.time()), "lang": lang, "model": model, "keys": keys,
                                    "usd": round(usd or 0, 5)}) + "\n")

    def over(self):
        return self.budget is not None and self.usd >= self.budget


def ask(system, items, schema, model, effort="low"):
    """One `claude -p` call: (structured answer, USD)."""
    from core import realclaude
    if os.environ.get("HUB_NO_MODEL"):   # tests: never the live model
        raise RuntimeError("the model is off (HUB_NO_MODEL)")
    claude = realclaude.find()
    if not claude:
        raise RuntimeError("claude is not found: install Claude Code and sign in")
    env = {**os.environ, "DISABLE_PROMPT_CACHING": "1"}   # each batch is read once: a cache write would cost the input twice
    if env.get("HUB_GATEWAY_STUB") and not env.get("CLAUDE_CODE_OAUTH_TOKEN"):
        # in a hub session realclaude.find() bypasses the wrapper that signs the call in with the gateway stub
        env["CLAUDE_CODE_OAUTH_TOKEN"] = env["HUB_GATEWAY_STUB"]
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8", newline="\n") as f:
        f.write(system)
        path = f.name
    try:
        r = subprocess.run([claude, "-p", "--model", model, "--effort", effort, "--tools", "",
                            "--setting-sources", "", "--strict-mcp-config", "--no-session-persistence",
                            "--output-format", "json", "--system-prompt-file", path,
                            "--json-schema", json.dumps(schema)],
                           input=json.dumps(items, ensure_ascii=False), capture_output=True, text=True,
                           encoding="utf-8", timeout=TIMEOUT, cwd=tempfile.gettempdir(),
                           env=env)
    finally:
        os.unlink(path)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"claude -p (rc {r.returncode}): {(r.stderr or r.stdout)[-300:]}") from None
    if not isinstance(out, dict):
        raise RuntimeError(f"claude -p: not an answer object: {r.stdout[:200]}")
    usd = out.get("total_cost_usd") or 0
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise RuntimeError(f"claude -p: {str(out.get('result'))[:300]}", usd)
    return out["structured_output"], usd


# --- writing ----------------------------------------------------------------------------------------------------------


def save(lang, got):
    """Put checked translations into the language's catalog and stamp them."""
    path = os.path.join(CATALOGS, f"{lang}.json")
    cat = tool._read(path, {})
    cat.update(got)
    tool._write(path, cat)
    tool.stamp(lang, sorted(got), root=CATALOGS)


def translate(lang, keys, spend, model=MODEL, jobs=4, notes=None, dry=False, log=print, chars=BATCH_CHARS,
              effort="low"):
    """Translate these keys of a language; returns the keys that did not pass the checks."""
    en, ru = catalogs()
    system = prompt(lang, fresh=all(k.startswith(GLOSSARY) for k in keys))   # choosing the terms: not from themselves
    notes = notes or {}

    def item(k, problem=None):
        x = {"k": k, "en": en[k]}
        if k in ru and len(json.dumps(en[k], ensure_ascii=False)) <= RU_UPTO:
            x["ru"] = ru[k]
        if k.startswith(GLOSSARY):
            x["note"] = glossary_notes().get(k, "")
            x["examples"] = examples(lang, en[k])
        if problem or notes.get(k):
            x["problem"] = "; ".join(p for p in (notes.get(k), problem) if p) + " — fix it in this translation"
        return x

    todo = {k: None for k in keys if k in en}
    failed = {}
    for attempt in range(TRIES):
        if not todo or spend.over():
            break
        parts = batches([item(k, p) for k, p in todo.items()], chars // (2 if attempt else 1), BATCH_KEYS)
        if dry:
            log(f"{lang}: {len(todo)} keys in {len(parts)} batches; the prompt:\n{system}")
            return {}
        log(f"{lang}: try {attempt + 1} — {len(todo)} keys, {len(parts)} batches")
        nxt = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
            running = {}
            queue = list(parts)
            while queue or running:
                while queue and len(running) < jobs and not spend.over():
                    part = queue.pop(0)
                    numbered = [{"i": n, **x} for n, x in enumerate(part)]
                    running[pool.submit(ask, system, numbered, SCHEMA, model, effort)] = part
                if not running:
                    nxt.update({x["k"]: "not asked: the budget is reached" for p in queue for x in p})
                    queue = []
                    break
                done, _ = concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)
                for fut in done:
                    part = running.pop(fut)
                    asked = [x["k"] for x in part]
                    try:
                        answer, usd = fut.result()
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                        usd = e.args[1] if isinstance(e, RuntimeError) and len(e.args) > 1 else 0
                        spend.add(lang, model, len(asked), usd)
                        why = f"the batch failed: {str(e.args[0] if e.args else e)[:160]}"
                        nxt.update({k: why for k in asked})
                        log(f"{lang}: {why}; again")
                        continue
                    spend.add(lang, model, len(asked), usd)
                    good = {}
                    by = {asked[x["i"]]: x for x in answer.get("items") or []
                          if isinstance(x, dict) and isinstance(x.get("i"), int) and 0 <= x["i"] < len(asked)}
                    for k in asked:
                        x = by.get(k) or {}
                        value = x.get("forms") if isinstance(en[k], dict) else x.get("text")
                        if isinstance(en[k], dict) and isinstance(value, dict):
                            value = {f: v for f, v in value.items() if v}
                        why = check(lang, en[k], value) if x else "no answer for this key"
                        if why:
                            nxt[k] = why
                        else:
                            good[k] = value
                    if good:
                        save(lang, good)
                    log(f"{lang}: +{len(good)}/{len(asked)} · ${spend.usd:.2f}")
        failed = nxt
        todo = nxt
    return failed


# "no defect" items the reviewer lists anyway: the whole remark says so (not a defect that happens to start with "OK")
NOT_A_DEFECT = re.compile(r"(^|[;.,]\s*)(no (real )?defect|no change needed)\W*$|^(fine|acceptable)( per glossary)?\W*$", re.I)


def review(lang, spend, keys=None, model=REVIEW_MODEL, jobs=4, log=print):
    """{key: problem} the reviewer found in the language's translations."""
    en, ru = catalogs()
    cat = tool._catalog(CATALOGS, lang) or {}
    name = i18n.NAMES.get(lang, lang)
    system = REVIEW_RULES.format(name=name, lang=lang, about=ABOUT) + \
        f"\n\nGlossary (English → {name}):\n" + glossary_lines(lang)
    items = [{"k": k, "en": en[k], **({"ru": ru[k]} if k in ru else {}), "tr": cat[k]}
             for k in (sorted(cat) if keys is None else keys) if k in en and k in cat and not k.startswith(GLOSSARY)]
    parts = batches(items, REVIEW_CHARS, REVIEW_KEYS)
    found = {}
    log(f"{lang}: review — {len(items)} keys, {len(parts)} batches")
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futs, queue = {}, list(parts)
        while queue or futs:
            while queue and len(futs) < jobs and not spend.over():   # at most jobs at once: the budget is seen between them
                part = queue.pop(0)
                futs[pool.submit(ask, system, part, REVIEW_SCHEMA, model)] = part
            if not futs:
                break
            done, _ = concurrent.futures.wait(futs, return_when=concurrent.futures.FIRST_COMPLETED)
            fut = next(iter(done))
            part = futs.pop(fut)
            try:
                answer, usd = fut.result()
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                spend.add(lang, model, len(part), e.args[1] if isinstance(e, RuntimeError) and len(e.args) > 1 else 0)
                log(f"{lang}: a review batch failed: {str(e)[:160]}")
                continue
            spend.add(lang, model, len(part), usd)
            asked = {x["k"] for x in part}
            for x in answer.get("items") or []:
                if isinstance(x, dict) and x.get("k") in asked and x.get("problem") and not NOT_A_DEFECT.search(x["problem"]):
                    found[x["k"]] = x["problem"]
            log(f"{lang}: review {len(found)} flagged · ${spend.usd:.2f}")
    return found


def sample(lang, n):
    """n keys of the language to review: half short page texts (where a wrong word or length shows most), half any."""
    import random
    en, _ = catalogs()
    cat = tool._catalog(CATALOGS, lang) or {}
    keys = sorted(k for k in cat if k in en and not k.startswith(GLOSSARY))
    short = [k for k in keys if k.startswith(("web.", "files.")) and isinstance(en[k], str) and len(en[k]) <= 30]
    rnd = random.Random(f"{lang}:{n}")
    first = rnd.sample(short, min(len(short), n // 2))
    taken = set(first)
    rest = [k for k in keys if k not in taken]
    return sorted(first + rnd.sample(rest, min(len(rest), n - len(first))))


def languages(args):
    stage = tool._read(os.path.join(TERMS or CATALOGS, tool.STAGE), {})
    if args.all or not args.langs:
        return [x for x in stage.get("ready") or [] if x not in (stage.get("translated") or [])]
    return args.langs


def main(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("langs", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--keys")
    p.add_argument("--jobs", type=int, default=4)
    p.add_argument("--budget", type=float)
    p.add_argument("--ledger")
    p.add_argument("--model", default=MODEL)
    p.add_argument("--review-model", default=REVIEW_MODEL)
    p.add_argument("--no-fix", action="store_true")
    p.add_argument("--report")
    p.add_argument("--sample", type=int)
    p.add_argument("--dry", action="store_true")
    p.add_argument("--catalogs")
    cmd = argv[0] if argv and argv[0] in ("glossary", "review") else "translate"
    args = p.parse_args(argv[1:] if cmd != "translate" else argv)
    if args.catalogs:
        global CATALOGS, TERMS
        TERMS, CATALOGS = TERMS or CATALOGS, os.path.abspath(args.catalogs)   # the core's stays the glossary's
    spend = Spend(args.budget, args.ledger)
    only = set(args.keys.split(",")) if args.keys else None
    en, _ = catalogs()
    left, report = {}, {}
    for lang in languages(args):
        if cmd == "glossary":
            keys = [k for k in en if k.startswith(GLOSSARY)]
            keys = [k for k, _ in tool.status(lang, root=CATALOGS) if k in keys] if not only else [k for k in keys if k in only]
            left[lang] = translate(lang, keys, spend, args.model, args.jobs, dry=args.dry, chars=10 ** 6,
                                   effort="high")   # the terms in one call: chosen together
        elif cmd == "review":
            keys = sorted(only) if only else (sample(lang, args.sample) if args.sample else None)
            found = review(lang, spend, keys, args.review_model, args.jobs)
            report[lang] = found
            if found and not args.no_fix:
                left[lang] = translate(lang, sorted(found), spend, args.model, args.jobs, notes=found, dry=args.dry)
        else:
            stale = [k for k, _ in tool.status(lang, root=CATALOGS)]
            gl = [k for k in stale if k.startswith(GLOSSARY)]
            if gl and not only:
                left[lang] = translate(lang, gl, spend, args.model, args.jobs, dry=args.dry, chars=10 ** 6,
                                       effort="high")   # the terms first, in one call
            keys = [k for k in stale if (not only and not k.startswith(GLOSSARY)) or (only and k in only)]
            left[lang] = {**left.get(lang, {}), **translate(lang, keys, spend, args.model, args.jobs, dry=args.dry)}
        if spend.over():
            print(f"the budget ${args.budget:.2f} is reached: stopped after {lang}", file=sys.stderr)
            break
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"review": report, "left": left, "usd": round(spend.usd, 4)}, f, ensure_ascii=False, indent=1)
    for lang, keys in left.items():
        for key, why in sorted(keys.items()):
            print(f"{lang}: {key}: did not pass — {why}", file=sys.stderr)
    print(f"spent: ${spend.usd:.2f}")
    return 1 if any(left.values()) or spend.over() else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
