// The panel search matches session names by the same rule as the native clients (hub-ios SearchNames and its checks in
// tools/favorites-check): the examples below are theirs, so the web and the apps find the same threads for the same query.
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fold, rank, rankFolded, otherLayout, matcher, setKeyboards } from "../../modules/web/static/js/names.js";

// the Russian layout of the language data (i18n/data/ru.json `keyboard`), as GET /api/keyboards hands it to the page
const RU = JSON.parse(readFileSync(new URL("../../i18n/data/ru.json", import.meta.url), "utf8")).keyboard.keys;
setKeyboards({ layouts: { ru: RU } });
// what these Latin keys type on the Russian layout: the Russian examples are spelled by their keys, not written here
const ru = (latin) => [...latin].map((c) => RU[c] ?? c).join("");

test("the layout: one Cyrillic letter for every key of the US letter rows, no letter twice", () => {
  assert.deepEqual(Object.keys(RU).sort(), [..."qwertyuiop[]asdfghjkl;'zxcvbnm,.`"].sort());
  assert.equal(new Set(Object.values(RU)).size, Object.keys(RU).length);
  assert.ok(Object.values(RU).every((c) => c.codePointAt(0) >= 0x400 && c.codePointAt(0) < 0x460), "Cyrillic letters only");
});

test("start of the name — 0, start of a word — 1, inside a word — 2", () => {
  assert.equal(rank("brush-sales", "bru"), 0);
  assert.equal(rank("brush-sales", "sal"), 1);
  assert.equal(rank("brush-sales", "ale"), 2);
  assert.equal(rankFolded(fold("brush-sales"), fold("Sal")), 1, "folded beforehand — the same rule");
});

test("no match or an empty query — no match", () => {
  assert.equal(rank("brush-sales", "x"), null);
  assert.equal(rank("brush-sales", ""), null);
  assert.equal(rank("brush-sales", "  "), null);
});

test("one letter — only the start of the name or of a word, not the middle", () => {
  assert.equal(rank("brush-sales", "b"), 0);
  assert.equal(rank("brush-sales", "s"), 1);
  assert.equal(rank("brush-sales", "r"), null);
  assert.equal(rank("atlas-usability", "s"), null);
  assert.equal(rank("atlas-usability", "s", 1), 2, "with inner 1 (the ⚡ destinations) one letter matches inside too");
});

test("case and the separators - _ . / and spaces do not matter: «hub dev» finds «hub-dev»", () => {
  assert.equal(rank("hub-dev", "Hub Dev"), 0);
  assert.equal(rank("hub-dev", "hub dev"), 0);
  assert.equal(rank("hub_dev.main", "dev main"), 1);
  assert.equal(rank("hub-dev", "hub   dev"), 0, "extra spaces are one boundary");
  assert.equal(rank("web/look", "web-look"), 0);
  assert.equal(fold("  Hub--Dev_ "), "hub dev");
});

test("diacritics and the two dots over the Russian e do not matter", () => {
  assert.equal(rank("Lago-Señal", "senal"), 1);
  assert.equal(rank(ru("`krf"), ru("tk")), 0, "the letter on ` folds to the one on t");
  assert.equal(rank("Café", "cafe"), 0);
});

test("a typo is not forgiven: only case, diacritics, separators and the layout do not matter", () => {
  assert.equal(rank("brush-sales", "bruon"), null);
  assert.equal(rank("hub-dev", "hbu"), null);
  assert.equal(rank("garden-ai", "gardn"), null);
});

test("a query of separators only finds nothing", () => {
  assert.equal(fold("-"), "");
  assert.equal(matcher("-")(["hub-dev", "-"]), null);
  for (const q of ["-", "_", ".", "/", ". /", "-_./"])   // "." is a letter of the other layout
    assert.equal(matcher(q)([ru(".kf"), "hub-dev", "hub.dev", "a/b"]), null, q);
});

test("«bru» typed on the Russian keys finds what «bru» finds, after every match of what was typed", () => {
  assert.equal(otherLayout(ru("bru")), "bru");
  assert.equal(otherLayout("ghbdtn"), ru("ghbdtn"));
  assert.equal(otherLayout(ru("bru").toUpperCase()), "bru", "capitals too");
  assert.equal(otherLayout("123"), null, "nothing to change — no second query");
  const m = matcher(ru("bru"));
  assert.equal(m(["brush-sales"]), 3, "start of the name in the other layout — after every typed match");
  assert.equal(m(["sales-brush"]), 4);
  assert.equal(m(["other"]), null);
  const names = ["sales-brush", "brush", "brush-business", "embrun", "brush-sales", "other"];
  const hits = (q) => names.filter((n) => matcher(q)([n]) !== null);
  assert.deepEqual(hits(ru("bru")), hits("bru"), "the same names");
  const t = matcher("t");   // a Russian letter on the Russian keys
  assert.deepEqual([ru("t;tlytdybr"), "tile-pin", "atlas-tools"].map((n) => t([n])), [3, 0, 1],
    "the other layout's match stands after the typed ones");
});

test("the best of a session's labels counts", () => {
  assert.equal(matcher("orbit")(["hub", "Personal hub Orbit"]), 1);
  assert.equal(matcher("hub")(["hub", "Personal hub Orbit"]), 0);
});

test("without layouts (an older server, no /api/keyboards) the search matches only what was typed", () => {
  try {
    for (const none of [null, undefined, {}, { layouts: {} }, { layouts: { ru: "not a map" } }]) {
      setKeyboards(none);
      assert.equal(otherLayout(ru("bru")), null);
      assert.equal(matcher(ru("bru"))(["brush-sales"]), null);
      assert.equal(matcher("bru")(["brush-sales"]), 0, "what was typed still matches");
    }
  } finally {
    setKeyboards({ layouts: { ru: RU } });
  }
});
