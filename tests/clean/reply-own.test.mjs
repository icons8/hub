// "Reply" on the owner's own words and on his answer to a card: the same button and quote path as on a session's reply.
// In Clean the button of an own bubble stands outside it, on its start side, so it never covers the time or the text.
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";

const root = path.join(import.meta.dirname, "..", "..", "modules", "web", "static");
const css = fs.readFileSync(path.join(root, "clean", "clean.css"), "utf8");
const feed = fs.readFileSync(path.join(root, "js", "feed.js"), "utf8");
const rule = (sel) => {
  const out = [];
  for (let i = css.indexOf(sel + " {"); i >= 0; i = css.indexOf(sel + " {", i + 1)) {
    if (i === 0 || "\n ".includes(css[i - 1])) out.push(css.slice(i, css.indexOf("}", i)));
  }
  assert.ok(out.length, sel);
  return out.join("\n");
};
const kind = (name) => feed.slice(feed.indexOf(`  ${name}: (it) =>`), feed.indexOf("\n  },", feed.indexOf(`  ${name}: (it) =>`)));

test("an own message and an answer to a card get the feed's Reply button with their text as the quote", () => {
  // words: what is on screen as the owner's text — a label, or a screen recording's own text without the app's note
  assert.match(kind("in"), /const words = cast \? cast\.text : shownText\(it\);/);
  assert.match(kind("in"), /if \(mine && S\.cur && words\) b\.append\(replyBtn\(t\("web\.feed\.reply"\), \{ session: S\.cur\.name, quote: words \}\)\)/,
    "the quote is what is on screen (a label), a message with only attachments has no button, as a session's");
  assert.match(kind("answer"), /replyBtn\(t\("web\.feed\.reply"\), \{ session: S\.cur\.name, quote: what \}\)/);
  assert.doesNotMatch(kind("in"), /return mine \|\| !S\.cur \? r :/, "a selected fragment of an own message can be quoted too");
});

test("on an own bubble the button stands outside it, on the start side, with a logical inset", () => {
  assert.match(rule("html.th-clean .row.mine > .b > .reply-btn"), /inset-inline-end: calc\(100% \+ 4px\)/);
  assert.match(css, /html\.th-clean \.row\.mine > \.b \{ position: relative;/);
  assert.doesNotMatch(rule("html.th-clean .row.mine > .b > .reply-btn"), /(^|[\s;{])(left|right):/);
});

test("on an answer to a card the button follows the line", () => {
  const b = rule("html.th-clean .row.answer > .b");
  assert.match(b, /position: relative/);
  assert.match(b, /justify-self: start/);
  assert.match(b, /padding-inline-end: 28px/);
});

test("the text of an own bubble and of an answer to a card is a text block the selection bar takes", () => {
  const sel = fs.readFileSync(path.join(root, "js", "quotesel.js"), "utf8");
  assert.match(sel, /querySelectorAll\("\.md, \.summary, \.own-text"\)/);
  assert.match(kind("in"), /if \(words\) b\.append\(el\("span", "own-text", words\)\)/);
  assert.match(kind("answer"), /b\.append\(el\("span", "own-text", t\("web\.feed\.answer"/);
});

test("the jump from a quote finds own words and answers to cards (quotefind.js), not only sessions' replies", () => {
  const main = fs.readFileSync(path.join(root, "js", "main.js"), "utf8");
  assert.match(main, /quoteSource\(olderOf\(name, live\)\.concat\(live\), msg, textsIn\)/);
});

test("the quote mark in a sent bubble is the speech cloud in both sets; the Reply action (button, selection bar) is the arrow", () => {
  const d = (f) => fs.readFileSync(path.join(root, "clean", f), "utf8").match(/ d="([^"]+)"/)[1];
  assert.equal(d("i/quote.svg"), d("id/quote.svg"));
  assert.equal(d("i/reply.svg"), d("id/reply.svg"));
  assert.equal(d("i/reply.svg"), d("reply.svg"));
  assert.notEqual(d("i/quote.svg"), d("i/reply.svg"));
  const items = JSON.parse(fs.readFileSync(path.join(root, "clean", "icons8.json"), "utf8")).icons.items;
  assert.equal(items.quote.commonName, "response");
  assert.equal(items.reply.commonName, "undo");
  const js = (f) => fs.readFileSync(path.join(root, "js", f), "utf8");
  assert.match(js("feed.js"), /line\.append\(icon\("quote", "reply"\)/, "without the theme the quote keeps the core's arrow");
  assert.match(js("quotesel.js"), /reply\.append\(icon\("reply"\)/);
  assert.match(js("themes.js"), /"copy", "reply", "quote"\]/);
  assert.match(js("dom.js"), /if \(alt && !kit\.icons\[name\]\) name = alt;/);
});
