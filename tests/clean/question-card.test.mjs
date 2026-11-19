// A question card in Clean: the own answer is one plate with the mic inside, a recording field mic is a level plate, "Answer"
// has no check mark; after the answer the error is a soft red plate, "sent" a gray line, a frozen header on --c-fill.
// Reads clean.css as text. Browserless.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8");
const rule = (sel) => {
  const i = css.indexOf("\n" + sel + " {");
  assert.ok(i >= 0, sel);
  return css.slice(i, css.indexOf("}", i));
};

test("the own answer is one plate on --c-fill, the field inside it without its own border", () => {
  const row = rule("html.th-clean form.ask-card .other-row");
  assert.match(row, /background: var\(--c-fill\)/);
  assert.match(row, /min-height: calc\(var\(--c-lh\) \+ 24px\)/);   // 44 at the default text size, grows with it
  assert.match(rule("html.th-clean form.ask-card .other-row textarea.other"), /border: 0;.*background: none/s);
  assert.match(rule("html.th-clean form.ask-card .other-row:has(textarea.other:focus)"), /inset 0 0 0 1px var\(--c-line-strong\)/);
});

test("the mic is a quiet 24 button inside the plate; while recording a 56x24 level plate", () => {
  assert.match(rule("html.th-clean form.ask-card .other-row > .btn.round.dictate:not(.stop)"), /width: 24px; height: 24px/);
  assert.match(rule("html.th-clean .btn.round.dictate.mini.stop"), /width: 56px; height: 24px/);
  assert.match(rule("html.th-clean .btn.round.dictate.mini.stop .icon"), /display: none/);
});

test("Answer goes without the check mark", () => {
  assert.match(rule("html.th-clean form.ask-card .btns .btn.primary > .icon"), /display: none/);
});

test("after the answer: the error on soft red 12/16, sent 14/20 gray, a frozen header on --c-fill", () => {
  const err = rule("html.th-clean .card .error-text:not(:empty)");
  assert.match(err, /background: var\(--c-alert-tint\)/);
  assert.match(err, /var\(--c-fs-s\)\/var\(--c-lh-s\)/);
  assert.match(rule("html.th-clean .card .sent-line"), /var\(--c-fs\)\/var\(--c-lh\).*color: var\(--c-t2\)/);
  assert.match(rule("html.th-clean .card.frozen .card-head"), /background: var\(--c-fill\)/);
});
