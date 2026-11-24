// Which row the quote of a reply leads to (main.js S.jumpQuote → quotefind.js quoteSource): the owner's own words and his
// answer to a card are sources too, an exact row wins over a longer one that contains the quote, only earlier rows count.
import test from "node:test";
import assert from "node:assert/strict";
import { quoteSource, quotableRow, quotedBy, variants } from "../../modules/web/static/js/quotefind.js";

const varsOf = (x) => variants(x.text, x.label, x.what);
const find = (rows, quote, seq = 100) => quoteSource(rows, { seq, quote }, varsOf);

test("the quote of the owner's own words leads to his bubble", () => {
  const own = { seq: 1, kind: "in", text: "Make the button blue" };
  assert.equal(find([own, { seq: 2, kind: "out", text: "Done" }], "Make the button blue"), own);
});

test("a session repeating the owner's words later does not take the jump from his bubble", () => {
  const own = { seq: 1, kind: "in", text: "Make the button blue" };
  const echo = { seq: 2, kind: "out", text: "You asked: 'Make the button blue'. Done, check it on the phone." };
  assert.equal(find([own, echo], "Make the button blue"), own);
});

test("the quote of an answer 'Yes' to a card leads to the answer row, not to a word inside a reply", () => {
  const answer = { seq: 1, kind: "answer", what: "Yes" };
  const reply = { seq: 2, kind: "out", text: "Yes, that can be done tomorrow" };
  assert.equal(find([answer, reply], "Yes"), answer);
});

test("a label is matched as it is on screen", () => {
  const own = { seq: 1, kind: "in", text: "/voice 1 2 3", label: "Voice: check the report" };
  assert.equal(find([own], "Voice: check the report"), own);
});

test("rows to other sessions, errors and later rows are not sources", () => {
  assert.equal(quotableRow({ kind: "in", to: "b", text: "x" }), false);
  assert.equal(quotableRow({ kind: "in", sender: "b", text: "x" }), false);
  assert.equal(quotableRow({ kind: "out", error: true, text: "x" }), false);
  assert.equal(find([{ seq: 5, kind: "in", text: "Make the button blue" }], "Make the button blue", 3), null);
});

test("a short quote is a whole word: 'yes' is not found inside 'eyes'", () => {
  assert.equal(find([{ seq: 1, kind: "out", text: "Look at my eyes" }], "yes"), null);
});

test("a file the session sent and the owner's message with only files are found by the quotes \"Reply\" puts on them", () => {
  const byFile = (rows, quote) => quoteSource(rows, { seq: 100, quote }, (x) => quotedBy(x));
  const mention = { seq: 1, kind: "out", text: "The totals go into report.md, I will send it." };
  const file = { seq: 2, kind: "file", name: "report.md", caption: null };
  const own = { seq: 3, kind: "in", text: "", attachments: [{ name: "plan.pdf" }, { name: "notes.txt" }] };
  assert.equal(byFile([mention, file, own], "report.md"), file);
  assert.equal(byFile([mention, file, own], "plan.pdf, notes.txt"), own);
  const captioned = { ...file, caption: "The March totals" };
  assert.equal(byFile([mention, captioned], "The March totals"), captioned);
  // a word picked from a sentence stays with the sentence, though a newer file is named after it
  assert.equal(byFile([mention, file], "totals"), mention);
  assert.equal(byFile([{ seq: 1, kind: "out", text: "the report is ready" }, { seq: 2, kind: "file", name: "report.pdf" }], "report").seq, 1);
  // a screen recording without words: "Reply" quotes its other files, not the video and its track
  const cast = { seq: 4, kind: "in", text: "", attachments: [{ name: "screencast-a1.mp4" }, { name: "notes.txt" }, { name: "screencast-a1.events.jsonl" }] };
  const older = { seq: 3, kind: "file", name: "notes.txt" };
  assert.equal(byFile([older, cast], "notes.txt"), cast);
});
