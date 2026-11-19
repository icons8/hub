// Schedule in the Clean theme: the back sign and the row chevron are Forma masks (not a text character or CSS borders),
// the back arrow is mirrored back in right-to-left, and the rules sit above the "Chat avatars" section (which only takes
// rules that start with `html.th-clean.`). Browserless: clean.css and base.css are read as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const STATIC = path.join(import.meta.dirname, "..", "..", "modules", "web", "static");
const css = fs.readFileSync(path.join(STATIC, "clean", "clean.css"), "utf8");
const base = fs.readFileSync(path.join(STATIC, "css", "base.css"), "utf8");
const at = (s) => css.indexOf(s);

test("the back sign and the row chevron are Forma masks, above the avatars section", () => {
  for (const sel of ["html.th-clean .btn.back::before", "html.th-clean .job > .fold > summary::after"]) {
    const i = at(sel);
    assert.ok(i > 0, sel);
    assert.ok(i < at('"Chat avatars"'), sel + " must be above the avatars section");
    assert.match(css.slice(i, i + 400), /url\("next-page\.svg"\)/);
  }
  assert.ok(fs.existsSync(path.join(STATIC, "clean", "next-page.svg")));
});

test("right-to-left: the Clean back arrow is not mirrored", () => {
  assert.match(css, /html\[dir="rtl"\]\.th-clean \.btn\.back::before \{ transform: none; \}/);
});

test("the classic back sign has a plain fallback before the alt-text form", () => {
  assert.match(base, /\.btn\.back::before \{ content: "‹"; content: "‹" \/ "";/);
});

test("no rules for the removed schedule header selectors", () => {
  assert.doesNotMatch(css, /sched-title \.crumb|who-when \.who/);
});
