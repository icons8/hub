// A panel search with nothing found is one line under the field, as the native apps' search ("Nothing found"): Clean
// used to swap the core's line for a block — a search icon on a tile, "No sessions with “zzz” in the name", "Check the
// spelling." and a "Clear search" button — an empty state the field's own cross and Esc already cover. clean.js and
// clean.css are read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const ROOT = path.join(import.meta.dirname, "..", "..");
const DIR = path.join(ROOT, "modules", "web", "static", "clean");
const css = fs.readFileSync(path.join(DIR, "clean.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
const js = fs.readFileSync(path.join(DIR, "clean.js"), "utf8");
const sidebar = fs.readFileSync(path.join(ROOT, "modules", "web", "static", "js", "sidebar.js"), "utf8");

test("the core writes the one line; Clean does not replace it", () => {
  assert.match(sidebar, /el\("p", "hint", t\("web\.sidebar\.nothing_found"\)\)/);
  assert.doesNotMatch(js, /emptySearch|ce-clear|ce-title|ce-hint|ce-icon|web\.clean\.no_match|web\.clean\.check_spelling/);
});

test("the line is centered secondary text in the body size, with no block around it", () => {
  const rule = [...css.matchAll(/(^|\n)([^{}\n][^{}]*)\{([^}]*)\}/g)].find((m) => m[2].trim() === "html.th-clean #list > p.hint");
  assert.ok(rule, "a rule for the line");
  assert.match(rule[3], /text-align: center/);
  assert.match(rule[3], /color: var\(--c-t2\)/);
  assert.match(rule[3], /font: 400 var\(--c-fs\)\/var\(--c-lh\)/);
  assert.doesNotMatch(rule[3], /background|border|display: grid/);
  assert.doesNotMatch(css, /\.ce-(icon|title|clear)/, "no styles left for the removed block");
});

test("the clear cross in the field stays", () => {
  assert.match(js, /hub\.el\("button", "clean-x"\)/);
  assert.match(css, /\.search:has\(#filter:placeholder-shown\) \.clean-x \{ display: none; \}/);
});
