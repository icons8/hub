// "Text size" in Clean: the theme draws its fonts in fixed pixels (UI text 14/20, small 12/16), so every size is that pixel
// value times --c-rel = --ui-scale / --ui-base, the ratio of the chosen step to the default one: exactly 1 by default (the
// design's pixels), larger with "Large" and "Extra large". Browserless: the rules of clean.css are read as text, the steps
// of the setting come from textscale.js; the same in a real browser is in tests/test_web_e2e.py (every view, both looks).
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const web = path.join(import.meta.dirname, "..", "..", "modules", "web", "static");
const css = fs.readFileSync(path.join(web, "clean", "clean.css"), "utf8");
const tokens = fs.readFileSync(path.join(web, "css", "tokens.css"), "utf8");
const scales = fs.readFileSync(path.join(web, "js", "textscale.js"), "utf8");
const steps = scales.match(/export const STEPS = \[DEFAULT_SCALE, ((?:"[\d.]+"(?:, )?)+)\]/)[1].match(/[\d.]+/g);
steps.unshift(scales.match(/const DEFAULT_SCALE = "([\d.]+)"/)[1]);

test("the ratio is exactly 1 at the default step and grows with each larger one", () => {
  const base = tokens.match(/--ui-base: ([\d.]+);/)[1];
  assert.equal(steps.length, 3, "Normal, Large, Extra large");
  assert.equal(steps[0], base);
  const rel = steps.map((s) => Number(s) / Number(base));
  assert.equal(rel[0], 1);
  assert.ok(rel[1] > 1.1 && rel[2] > rel[1] * 1.1, rel.join(" "));
  assert.match(css, /--c-rel: calc\(var\(--ui-scale\) \/ var\(--ui-base\)\);/);
  assert.match(tokens, /--ui-scale: var\(--ui-base\);/);
});

test("the pixel sizes of the design are only in the tokens, a font and its line height follow the ratio", () => {
  const defs = [...css.matchAll(/(--c-(?:fs|lh)[\w-]*): ([^;]*);/g)];
  assert.ok(defs.length >= 7, "the tokens of Clean's sizes");
  for (const [, name, value] of defs) assert.match(value, /^calc\(\d+px \* var\(--c-rel\)\)$/, name);
  const bad = [];
  for (const m of css.matchAll(/(?:font(?:-size)?|(?<![-\w])line-height):[^;}]*/g)) {
    const decl = m[0];
    if (!/\d(px|rem|pt)\b/.test(decl) || /--c-rel|--c-fs|var\(--row,/.test(decl)) continue;
    if (decl === "font-size: 17px" && css.slice(Math.max(0, m.index - 60), m.index).includes(".av.emoji")) continue;   // an emoji in a fixed plate
    bad.push(decl);
  }
  assert.deepEqual(bad, []);
});

test("the elements that hold a line of text at a fixed height grow with it", () => {
  // a group header (24 with a 16 line), the "Brief | Detailed" switch and its buttons, badges and chips, the buttons of the
  // header, the queue and recap buttons, the status line and its tab: with a larger font a fixed height would cut the line or
  // leave it outside its plate (the reply card has no fixed height: it grows with its quote)
  const sels = [".sgroup-h", ".seg", ".seg button", ".c-n", ".gcount", ".fork-toggle", ".dock .status",
    "main > .head :is(.pill, .chip)", "main > .head .menu-wrap > .menu-btn", "main > .head > .btn:not(.quiet)",
    ".clean-queue .strip-row .btn", ".clean-queue .strip-toggle", ".clean-recap .btn", ".dock .status > .stat-left"];
  for (const sel of sels) {
    const at = css.search(new RegExp("^\\s*html\\.th-clean " + sel.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + " \\{", "m"));
    assert.ok(at >= 0, sel);
    const rule = css.slice(at, css.indexOf("}", at));
    assert.match(rule, /(?<![-\w])height: calc\([^;]*var\(--c-rel\)\)/, sel);
  }
});
