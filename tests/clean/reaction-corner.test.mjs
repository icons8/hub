// A reaction badge in Clean, as the native ReactionChip: opaque (page background, never see-through over a bubble) and,
// on an own message, hanging on the bubble's bottom corner at the line end (4px out past the end, 6px down; on the right
// in a left-to-right page, on the left in a right-to-left one). With the picker open the row stays under the text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8").replace(/\r\n/g, "\n");   // a Windows checkout: CRLF
// all declarations of the exact selector, joined
const rule = (sel) => {
  const out = [];
  for (let i = css.indexOf(sel + " {"); i >= 0; i = css.indexOf(sel + " {", i + 1)) {
    if (i === 0 || "\n ".includes(css[i - 1])) out.push(css.slice(i, css.indexOf("}", i)));
  }
  assert.ok(out.length, sel);
  return out.join("\n");
};

test("the badge is opaque: the page background, also under hover", () => {
  const chip = rule("html.th-clean .react-chip");
  assert.match(chip, /background: var\(--background\)/);
  assert.doesNotMatch(chip, /background: none/);
  assert.match(rule("html.th-clean .react-chip:hover"), /var\(--background\)/);
});

test("on an own message the badge hangs on the bubble's bottom corner at the line end, 4px out, 6px down", () => {
  const box = rule("html.th-clean .row.mine > .b > .reactions:has(> .react-chip):not(:has(> .react-pick))");
  assert.match(box, /position: absolute/);
  assert.match(box, /inset-inline-end: -4px/);
  assert.match(box, /bottom: -6px/);
  assert.doesNotMatch(box, /(^|[\s;{])(left|right):/, "a logical side, so a right-to-left page mirrors it");
  const bubble = rule("html.th-clean .row.mine > .b:has(> .reactions > .react-chip):not(:has(> .reactions > .react-pick))");
  assert.match(bubble, /position: relative/);
  assert.match(bubble, /padding-bottom: 24px/);   // the badge never covers the last line
});

test("an own message has no \"Reaction\" control: neither the word nor an icon, only the core's badge", () => {
  assert.match(rule("html.th-clean .row.mine > .b .react-add"), /display: none/);
});

test("the open picker is not pinned to the corner", () => {
  assert.doesNotMatch(css, /\.reactions:has\(> \.react-pick\)[^{]*\{[^}]*position: absolute/);
  // and the hidden "Reaction" beside a 👍 that leads to the answer does not take the badge off the corner
  assert.doesNotMatch(css, /\.reactions:has\(> \.react-chip:only-child\)/);
});

// "Reply" and "Reaction" stand at the message, not at the feed's edge: without a box of their own the corner buttons took
// the whole row's, as wide as the feed, and on a wide screen sat far right of the text
const root = path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean");

test("the corner buttons are placed in the message block, the avatar keeps its column", () => {
  assert.match(rule("html.th-clean .row > .b:has(> .reply-btn)"), /position: relative/);
  assert.match(rule("html.th-clean .row > .b:has(> .reply-btn) > .byline .av"), /inset-inline-start: calc\(-32px - 12px\)/);
  const box = rule("html.th-clean .row > .b > .reply-box > .meta:has(> .reply-btn)");   // a reply to you: the box is feed-wide
  assert.match(box, /position: relative/);
  assert.match(box, /max-width: calc\(var\(--measure\) \* 14 \/ 12\)/);
  assert.match(box, /padding-inline-end: 28px/);
});

test("the name row keeps the buttons' room as an empty last column, so the time stays right after the name", () => {
  // 20 + the 8 column gap = 28 for Reply, twice that with Reaction; a padding on the name would push the time away from it
  assert.match(rule("html.th-clean .row.out > .b:has(> .byline):has(> .reply-btn)"), /grid-template-columns: auto minmax\(0, 1fr\) 20px;/);
  assert.match(rule("html.th-clean .row.out > .b:has(> .byline):has(> .reply-btn ~ .react-add)"),
    /grid-template-columns: auto minmax\(0, 1fr\) 20px 20px;/);
  assert.doesNotMatch(css, /> \.byline \{ padding-inline-end/);
});

test("Reaction under a session's reply is an icon button left of Reply, seen when Reply is", () => {
  const sel = "html.th-clean .row > .b > .reply-btn ~ .react-add";
  const btn = rule(sel);
  assert.match(btn, /position: absolute/);
  assert.match(btn, /top: -2px; inset-inline-end: 28px; width: 24px; height: 24px/);   // Reply: at the end, 24 wide, 4 apart
  assert.match(btn, /font-size: 0/);
  assert.match(btn, /opacity: 0/);
  assert.match(rule(sel + "::before"), /mask: url\("react\.svg"\) center \/ 16px no-repeat/);
  assert.match(css, /\.react-add\[aria-expanded="true"\] \{ opacity: 1; \}/);
  assert.match(css, /@media \(hover: none\) \{\n  html\.th-clean \.row > \.b > \.reply-btn ~ \.react-add \{ opacity: 1;/);
  const icons = JSON.parse(fs.readFileSync(path.join(root, "icons8.json"), "utf8"));
  assert.equal(icons.icons.items.react.commonName, "happy");
  assert.ok(fs.readFileSync(path.join(root, "react.svg"), "utf8").startsWith("<svg"));
  assert.match(fs.readFileSync(path.join(root, "clean.js"), "utf8"), /\.reply-btn ~ \.react-add:not\(\[title\]\)/);   // the word goes to the tooltip
});

test("the emoji's tuned offsets hold for Apple's emoji font only and give way to a theme's own `[data-emoji] { transform }`", () => {
  const face = rule(":where(html.th-clean-apple-emoji .react-chip > .react-emoji:not(.kit-img))");
  assert.match(face, /display: inline-block/);          // an inline box takes no transform
  assert.match(face, /transform: translate\([-\d.]+em, [-\d.]+em\)/);
  assert.doesNotMatch(css, /\.react-emoji[^{]*\{[^}]*[^-]translate:/);   // `translate` would add up with the theme's transform
  // every rule that moves the emoji: only with Apple's emoji font, and with no weight of its own, so any theme rule wins
  const moves = [...css.matchAll(/^([^\n{]*\.react-emoji[^\n{]*)\{[^}]*transform:/gm)].map((m) => m[1]);
  assert.ok(moves.length > 1);
  for (const sel of moves) assert.match(sel, /^:where\(html\.th-clean-apple-emoji /, sel);
  assert.match(css, /:where\(html\.th-clean-apple-emoji \.react-chip > \.react-emoji\[data-emoji\^="👍"\]\) \{ transform: translate\(/);
});
