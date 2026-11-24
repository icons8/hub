// "Copy" beside "Reply" in Clean: a reply to you has both buttons on the line of its name (the head of the reply box), and the text
// of that line stops short of them, 28 for each.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8");

test("the name line of a reply to you leaves room for \"Reply\" and \"Copy\"", () => {
  const sel = "html.th-clean .row > .b > .reply-box > .meta:has(> .copy-btn)";
  const i = css.indexOf(sel + " {");
  assert.ok(i >= 0);
  assert.match(css.slice(i, css.indexOf("}", i)), /padding-inline-end: 56px/);
});
