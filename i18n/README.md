# Interface languages

Everything a person reads — the page, "Files", the bundled plugins, the template of an agent's CLAUDE.md (next — Telegram
and the output of `hub`) — comes from the catalogs in this folder, one file per language. The source is English
(`en.json`). The prompts of the model (the addendum to sessions, the rules, the core's messages to sessions) are not
translated through catalogs: they are written right in the code.

Languages: `en`, `ru`, `zh-CN`, `es`, `hi`, `pt-BR`, `fr`, `de`, `ja`, `ko`, `ar`, `tr`, `uk`.

- `stage.json` `translated` — must be complete and fresh: `en`, `ru`.
- `stage.json` `ready` — the languages the hub speaks (the choice in "Settings → General", the browser, the owner's
  language): all thirteen. Arabic writes right to left: the server gives its pages `dir="rtl"`, the styles use logical
  properties (`margin-inline-start`, `inset-inline-end`, `text-align: start`), so the page mirrors; code stays left to
  right, and what people write takes the direction of its first letter.
- Between releases a ready language may lack a new phrase (English shows); a release may not — it carries every phrase
  in every ready language (`tools/i18n.py check --release`, `tools/publish.sh` code 10).

## How the language is chosen

In order, the first one named decides (`core/i18n.py` `human`):

1. `language` in hub.json (`"language": "de"`; empty — none).
2. The owner's language by his messages (the script: Cyrillic — ru/uk, kana, hangul…; Latin — the language is not known).
3. A hub whose history began before the languages — Russian (the `ui_lang_default` mark in the state, set by the core at
   start).
4. The browser's language (`Accept-Language`) — for the page only.
5. English.

A language that is not ready or not known, at any step — English. No key in a language — the English text; none in
English either — the key itself (the tests see it). The pseudo-language `xx` (only `"language": "xx"`) — English in
`⟦…⟧`: with it every string that goes around the catalog is visible.

## Format

A flat JSON, keys `area.thing` (`web.sidebar.find`), sorted:

```json
{
  "web.sidebar.find": "Find a session",
  "web.time.hours_ago": {"one": "{count} hour ago", "other": "{count} hours ago"}
}
```

- `{name}` — a substitution, the same in all languages; `{{` and `}}` — the braces themselves.
- Plural forms — by [CLDR](https://cldr.unicode.org/index/cldr-spec/plural-rules), chosen by `count`: English —
  `one`, `other`; Russian and Ukrainian — `one`, `few`, `many`, `other`; Chinese, Japanese, Korean — `other` only. Do not
  cut a phrase into pieces: the number is inside the form, as a whole.

## How to use

Python: `from core import i18n` → `i18n.tr(lang, "web.srv.x", name=…)` (the request's language), `i18n.t(…)` (the
process's language).

The page (`modules/web/static/js`): `import { t, locale } from "./i18n.js"` → `t("web.x.y", { count: n })`; dates —
`toLocaleString(locale, …)`. "Files" — `/web/js/i18n.js`. Page plugins — `hub.t`, `hub.lang`, `hub.locale` (Plugin API
1.25; a plugin keeps its own strings). The static text of `index.html` — `data-i18n="key"` (the element's text) and
`data-i18n-<attribute>="key"`: the server sends the page already in its language. The key is whole in the code's text
(`t("a.b")`), not built from parts: the checks look for the calls by text.

## A new string

1. The key and the English text — in `en.json`, the Russian — in `ru.json`; in the code — `t()`.
2. The other languages — before the release: `tools/translate.py --all` (below), or by hand and then
   `tools/i18n.py stamp <language> <key>`.
3. `tools/i18n.py check` → `translations: OK`; the test — `tools/test.py test_i18n`.

If you change an English text, its translations are stale: for ru it is an error until it is translated and marked
(`stamp ru <key>`); for the others `tools/i18n.py drop` removes the stale one (English shows).

## Translating

`tools/translate.py --all` translates what the ready languages lack or have stale (`tools/i18n.py status`): batches of
keys through `claude -p` (`claude-opus-5-5`; any signed-in Claude Code), with the English, the Russian of short texts
(the same author — it settles an ambiguous word) and the glossary. Every answer is checked by script before it is
written — the same `{placeholders}`, the plural forms of the language, flags, `hub` commands, paths, URLs, `code` and
tags kept verbatim, the letters of the language's script; a failing key is asked again with the reason. `--budget USD`
stops it, `--ledger FILE` records every call's price. `tools/translate.py review <language>` has a second model
(`claude-sonnet-5-5`) read the translations and translates again what it flags, with its note.

**The glossary** is the `glossary.*` keys of every catalog: one term per concept of the hub (session, executor,
direction, card…), each language its own column, stamped like any other string; `glossary.json` says what each term
means (English only, for the translator). `tools/translate.py glossary <language>` chooses a language's terms from its
earlier translations; a changed term is fixed in the other strings by `review`.

## Checks

- **Complete** (`tools/i18n.py check`): ru and en — all keys; every language — the same substitutions, its own plural
  forms, the letters of its script, nothing extra; the CLAUDE.md template (`agent_file.*`) — complete in every ready
  language.
- **Fresh** (`sync.json`): for a language and a key — the hash of the English text it was translated from.
- **Used**: every key the code asks for is in en and ru; the page keys (`web.*`, `files.*`) — only the used ones.
- **Ratchet** (`pending.json`): in the texts for a person (the page, "Files", Telegram, `bin/hub`) the Russian string
  literals only go down; comments and docstrings are not counted. A line where Russian is needed on purpose carries the
  `i18n-ok` mark; the words the core recognises and the earlier Russian forms live in the language data instead (below).

## Language data (`data/`)

`data/<language>.json` is not a catalog: nothing in it is shown, the core reads with it (`core/langdata.py`). Sections:
`words` (the owner's and the sessions' words the core acts on: "yes", the hub's name, a "Lessons" heading, generic form
labels, "no data"), `watch` (the phrases of the watcher's rules, regex fragments), `translit` (letters to Latin for
executor names), `letters` (what tells a language apart, the letters search treats as one), `self_name` (the language's
name in itself) and, only in the language the core spoke before its texts were English, `legacy` — the earlier forms of
the core's own texts (labels, headers, reason prefixes, sender identifiers) that journals and state of every hub hold.

The core recognises the union over all languages. A `legacy` value never changes (tests/test_langdata.py keeps a hash of
each): the code reads it back from old journals. A change to a data file runs the whole test suite (core/testsel.py).
