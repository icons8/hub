// The "In background" chip in the session header (Clean): its text sits between equal paddings. The chip may shrink to a floor, but
// the floor is a few letters (ch) and not a fixed px width: 72px was wider than a bare "In background" (49px), the text stuck to the
// left and the chip had an empty strip on its right (the owner: "why is there padding on the right inside the badge"). Browserless:
// the rule is read from clean.css as text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8");
const rules = (sel) => {
  const out = [];
  for (let i = css.indexOf(sel + " {"); i >= 0; i = css.indexOf(sel + " {", i + 1)) {
    if (i === 0 || "\n ".includes(css[i - 1])) out.push(css.slice(i, css.indexOf("}", i)));
  }
  return out;
};

test("the shrink floor of the background chip does not exceed its text", () => {
  const wide = rules("  html.th-clean main > .head .clean-bg").filter((r) => /flex: 0 100 auto/.test(r));
  assert.equal(wide.length, 1, "one rule sets the chip's shrink in the wide header");
  const floor = /min-width: ([^;]+);/.exec(wide[0])[1];
  assert.match(floor, /ch/, `the floor is in letters: ${floor}`);
  assert.doesNotMatch(floor, /^\d+px$/, `a fixed px floor is wider than a short label: ${floor}`);
});

test("the chip has no fixed width or padding-right on top of the core's chip padding", () => {
  for (const r of rules("html.th-clean main > .head .clean-bg").concat(rules("  html.th-clean main > .head .clean-bg"))) {
    if (/width: 24px/.test(r)) continue; // the icon mode of a narrow header: no text at all
    assert.doesNotMatch(r, /(?<![-\w])(?:min-)?width: \d+px/, r);
    assert.doesNotMatch(r, /padding-right|padding-inline-end/, r);
  }
});
