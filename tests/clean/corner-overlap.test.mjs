// Things that stood on top of each other in Clean (audit of the page, a desktop and a 390 phone, on a copy of the live journal):
// a plugin's button next to "Reply" on the owner's answer to a card sat on "Telegram · 01:27" / on the tick of a short answer; on a
// phone the header's status pill was squeezed to "as…" by a long session name, and the title of a screen to "Schedu…" by a long pill.
// Reads clean.css as text. Browserless.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.join(import.meta.dirname, "..", "..", "modules", "web", "static", "clean", "clean.css"), "utf8").replace(/\r\n/g, "\n");
const rule = (sel) => {
  const out = [];
  for (let i = css.indexOf(sel + " {"); i >= 0; i = css.indexOf(sel + " {", i + 1)) {
    if (i === 0 || "\n ".includes(css[i - 1])) out.push(css.slice(i, css.indexOf("}", i)));
  }
  assert.ok(out.length, sel);
  return out.join("\n");
};

test("an answer to a card keeps 28 of room per button after its text: Reply alone, a plugin's button next to it, two of them", () => {
  assert.match(rule("html.th-clean .row.answer > .b"), /padding-inline-end: 28px/);
  assert.match(rule("html.th-clean .row.answer > .b:has(> .reply-btn ~ button)"), /padding-inline-end: 56px/);
  assert.match(rule("html.th-clean .row.answer > .b:has(> .reply-btn ~ button ~ button)"), /padding-inline-end: 84px/);
});

test("on a phone the status pill is never shrunk (its word stays whole, at most half the row), the title is what gives way", () => {
  const pill = rule("  html.th-clean main > .head > .pill");
  assert.match(pill, /flex: none; max-width: 50%/);
  assert.match(pill, /text-overflow: ellipsis/);
  assert.doesNotMatch(pill, /flex: 0 20 auto/, "a shrinkable pill was cut to 'as…' by a long session name");
  assert.match(rule("  html.th-clean main > .head h2"), /flex: 0 1 auto/);
});

test("the collapse chevron of an opened card is a corner button like Reply (24, a 16 icon) at the top right, with room left of it for a plugin's button", () => {
  const head = rule('html.th-clean .chat-group > .b > .chat-head[aria-expanded="true"]');
  assert.match(head, /width: 24px; min-height: 0; height: 24px/, "32 beside a 24 star read as two sizes and covered the star");
  assert.match(head, /top: 10px; inset-inline-end: 6px/);
  assert.match(rule('html.th-clean .chat-group > .b > .chat-head[aria-expanded="true"] > .chat-chev'), /width: 16px; height: 16px; -webkit-mask-size: 16px; mask-size: 16px/);
  // the card's text stops short of the button (6 + 24 + gap); a plugin that adds a star one place further out widens it itself
  assert.match(rule('html.th-clean .chat-group:has(> .b > .chat-head[aria-expanded="true"]) > .b'), /padding: 12px 48px 12px 8px/);
});
