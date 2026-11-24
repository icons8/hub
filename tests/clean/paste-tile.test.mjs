// A long paste in the feed (a card with its first words and size) in Clean: its title is at the size of all the text (14/20, not the
// core's 14.93/18.67), and the card keeps its 64 height, which two lines of that title fill only with 3 above and below.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8");
const rule = (sel) => {
  const i = css.indexOf(sel + " {");
  assert.ok(i >= 0, sel);
  return css.slice(i, css.indexOf("}", i));
};

test("the card's title is 14/20 as all the text of the theme", () => {
  assert.match(rule("html.th-clean .paste-tile .pt-title"), /font: var\(--c-fs\)\/var\(--c-lh\) var\(--font\)/);
});

test("the card keeps its height: 3 above and below for two lines of 20, the size line and the gap", () => {
  assert.match(rule("html.th-clean .attach-list a.paste-tile"), /padding-block: 3px/);
});
