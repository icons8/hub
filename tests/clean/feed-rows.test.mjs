// Service rows of the feed in Clean: the step name in the text font, a turn error and a notification as a 32 tile in the avatar
// column (the urgent one on the soft red), a question with its own icon, the "New" divider in red, the lightbox buttons 32.
// Reads clean.css as text. Browserless.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");
const read = (f) => fs.readFileSync(path.join(root, f), "utf8");
const css = read("clean.css");
const rule = (sel) => {
  const i = css.indexOf("\n" + sel + " {");
  assert.ok(i >= 0, sel);
  return css.slice(i, css.indexOf("}", i));
};

test("the step name is in the text font, only the path or command is monospaced", () => {
  assert.match(rule("html.th-clean .tool .call b"), /font-family: var\(--font\)/);
  assert.match(rule("html.th-clean .sub-step > summary b"), /font-family: var\(--font\)/);
});

test("a turn error is a 32 tile on the soft red with a 16 cross, the text on the tile's axis", () => {
  const g = rule("html.th-clean .row.out.err > .g");
  assert.match(g, /height: 32px/);
  assert.match(g, /background: var\(--c-alert-tint\)/);
  assert.match(rule("html.th-clean .row.out.err > .g::before"), /width: 16px; height: 16px/);
  assert.match(rule("html.th-clean .row.out.err > .b"), /padding-top: 6px/);
});

test("a notification, a question and terminal input get a gray 32 tile with an icon; the urgent one is red on soft red", () => {
  const ROWS = 'html.th-clean .row:is(.notify, [data-key*="ask"], [data-key*="terminal"])';
  assert.match(rule(`${ROWS} > .g`), /width: 32px; height: 32px/);
  assert.match(rule(`${ROWS} > .g::before`), /i\/bell\.svg/);
  assert.match(rule('html.th-clean .row[data-key*="ask"] > .g::before'), /question\.svg/);
  assert.match(rule("html.th-clean .row.notify.err > .g"), /--c-alert-tint/);
  assert.match(rule("html.th-clean .row.notify.err > .g::before"), /var\(--destructive\)/);
  assert.ok(fs.existsSync(path.join(root, "question.svg")));
  assert.equal(JSON.parse(read("icons8.json")).icons.items.question.commonName, "question-mark");
});

test("the New divider stays the core's own (a solid plate), Clean does not restyle it; the jump counter is red", () => {
  assert.doesNotMatch(css, /\.row\.newdiv/);
  assert.match(rule("html.th-clean main > .btn.jumpnew .n"), /background: var\(--c-alert\)/);
});

test("the lightbox buttons are 32 r8 without a plate on a computer, the hover only for a fine pointer", () => {
  assert.match(rule("html.th-clean .lb-btn"), /width: 32px; height: 32px; border-radius: 8px; background: none/);
  assert.doesNotMatch(css, /\nhtml\.th-clean \.lb-btn:hover/);   // only inside @media (hover: hover)
});

test("sizes follow the text size setting: no fixed 14/20 or 12/16 font in the new rows", () => {
  for (const sel of ["html.th-clean .row.sys.answer", "html.th-clean .mention-lead", "html.th-clean .lb-bar"])
    assert.doesNotMatch(rule(sel), /\b1[24]px\/(16|20)px/, sel);
});

test("the copy button's result is read from the core's data-state, not from the icon inside", () => {
  const md = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "js", "markdown.js"), "utf8");
  for (const s of ["copy", "ok", "fail"]) assert.match(md, new RegExp(`t\\("web\\.markdown\\.[a-z_]+"\\), "${s}"\\)`), s);
  const all = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8");
  assert.match(all, /\.md \.code-copy:is\(\[data-state="ok"\], \[data-state="fail"\]\) \{/);
  assert.doesNotMatch(all, /code-copy:has\([^)]*\[style/);
});
