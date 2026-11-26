// Code blocks in Clean: the copy button is always there, not only under the cursor (a person looking for it should not have
// to hover every block first), and code keeps its lines — a long command scrolls sideways instead of wrapping into a shape
// that is no longer the command. The core draws the button: one icon, after a click a check mark in its place, and the word
// only in the button's title and aria-label; the theme adds no visible word. Browserless: checks the text of clean.css,
// clean.js and the core's markdown.js.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const web = path.join(import.meta.dirname, "..", "..", "modules", "web", "static");
const css = fs.readFileSync(path.join(web, "clean", "clean.css"), "utf8");
// top-level rules only: a rule inside @media (hover: hover) does not count for a phone or a keyboard
const top = css.replace(/@media[^{]*\{(?:[^{}]*\{[^}]*\})*\s*\}/g, "");
const rule = (sel) => {
  const out = [];
  for (let i = top.indexOf(sel + " {"); i >= 0; i = top.indexOf(sel + " {", i + 1)) {
    if (i === 0 || "\n ".includes(top[i - 1])) out.push(top.slice(i, top.indexOf("}", i)));
  }
  assert.ok(out.length, sel);
  return out.join("\n");
};
const px = (decl, prop) => Number((decl.match(new RegExp(`${prop}: (\\d+)px`)) || [])[1]);

test("the copy button of a code block is visible without hovering", () => {
  assert.match(rule("html.th-clean .md .code-copy"), /opacity: 1/);
});

test("code keeps its lines: no wrapping, sideways scroll", () => {
  const pre = rule("html.th-clean .md .code-box > pre");
  assert.match(pre, /white-space: pre;/);
  assert.match(pre, /overflow-wrap: normal/);
  assert.match(pre, /overflow-x: auto/);
});

test("text never slides under the button: the room for it is outside the scrolled area", () => {
  // a scrolled block shows an end padding only at the scroll end; a transparent end border stays beside the scrollport
  const btn = px(rule("html.th-clean .md .code-copy"), "width"), pre = rule("html.th-clean .md .code-box > pre");
  const room = Number((pre.match(/border-inline-end: (\d+)px solid transparent/) || [])[1]);
  assert.ok(btn && room >= btn + 8, `button ${btn}px, room ${room}px`);
  assert.doesNotMatch(pre, /padding-inline-end/);
  // the core's touch-screen room for the button (an end padding of 40) would come on top of the border
  assert.match(css, /@media \(hover: none\) \{ html\.th-clean \.md \.code-box > pre \{ padding-inline-end: var\(--space-3\); \} \}/);
});

test("a long line fades out before the button, so the block reads as scrollable", () => {
  // a mask on the text, ending where the room for the button starts: over short lines it is invisible
  const pre = rule("html.th-clean .md .code-box > pre"), fade = rule("html.th-clean .md .code-box:not(.code-inline) > pre");
  const room = Number((pre.match(/border-inline-end: (\d+)px/) || [])[1]);
  assert.match(fade, new RegExp(`mask-image: linear-gradient\\(to right, #000 calc\\(100% - \\d+px\\), transparent calc\\(100% - ${room}px\\)\\)`), "the fade ends at the button's room");
  assert.match(rule('html[dir="rtl"].th-clean .md .code-box:not(.code-inline) > pre'), /mask-image: linear-gradient\(to left, #000/);
});

test("the backing is one coat on the box, under the fade and the button alike: --muted is translucent", () => {
  // a plate of --muted laid over a pre that is already --muted painted it twice: a darker band ending in a hard seam at the
  // button's room. The box carries the backing, the pre none, and no gradient of --muted is drawn over the block
  assert.match(rule("html.th-clean .md .code-box:not(.code-inline)"), /background: var\(--muted\)/);
  assert.match(rule("html.th-clean .md .code-box:not(.code-inline) > pre"), /background: none/);
  assert.doesNotMatch(css, /\.code-box[^{}]*::after/);
});

test("the button shows no word: the check mark takes the icon's place, the word stays for screen readers", () => {
  assert.doesNotMatch(css, /code-copy[^{}]*\{[^}]*content:/);
  assert.doesNotMatch(fs.readFileSync(path.join(web, "clean", "clean.js"), "utf8"), /code-copy/);
  const md = fs.readFileSync(path.join(web, "js", "markdown.js"), "utf8");
  assert.match(md, /show\(icon\("allow"\), t\("web\.markdown\.copied"\), "ok"\)/);
  assert.match(md, /btn\.setAttribute\("aria-label", title\)/);
});
