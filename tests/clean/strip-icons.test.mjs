// The strip above the field: every icon in it (the queue sign, "Send now", the trash, the folder in the tokens tab) is drawn in the
// same 12 box. Reads clean.css as text. Browserless.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8").replace(/\r\n/g, "\n");
const block = (sel) => {
  const i = css.indexOf(sel + " {");
  assert.ok(i >= 0, sel);
  return css.slice(i, css.indexOf("}", i));
};
const boxes = (text) => [...text.matchAll(/\b(?:width|height): (\d+)px|center \/ (\d+)px/g)].map((m) => Number(m[1] || m[2]));

test("the queue sign, \"Send now\" and the trash are the folder's size", () => {
  const folder = boxes(css.slice(css.indexOf(".stat-dir::before {"), css.indexOf("}", css.indexOf(".stat-dir::before {"))));
  assert.deepEqual([...new Set(folder)], [12]);
  for (const sel of ["html.th-clean .clean-queue .strip-row::before", "html.th-clean .clean-queue .btn.clean-now::before, html.th-clean .clean-queue .btn.clean-del::before"]) {
    assert.deepEqual([...new Set(boxes(block(sel)))], [12], sel);
  }
});

test("the \"more\" row's text stays under the other rows' text: 12 side padding + 12 icon + 8 gap", () => {
  assert.match(block('html.th-clean .clean-queue .strip-row[data-clean="more"]'), /padding-inline-start: 32px/);
});
