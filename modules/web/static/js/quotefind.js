import { castOf } from "./cast.js";

// Which feed row a reply's quote came from — pure, testable without a browser (main.js S.jumpQuote, feed.js textsIn).
// A quote is matched against a row's text variants: raw, without markdown (a link keeps only its words), paths as
// short names (the "Short paths" checkbox), what is on screen (a label). A long quote is matched ignoring spaces and
// case (line breaks on screen differ); a short one ("yes", "ok") as whole words: otherwise "yes" would be found inside "eyes"
const spaced = (s) => String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
const plain = (s) => s.replace(/!?\[([^\]]*)\]\([^)]*\)/g, "$1").replace(/[*_~`#>|]/g, "");
const bare = (s) => s.replace(/(?:[^\s/()[\]«»"'`]*\/)+(?=[^\s/])/g, "");
const SHORT = 16;

export function variants(...texts) {
  const out = [];
  for (const t of texts) { const raw = String(t || ""), seen = plain(raw); if (raw) out.push(raw, seen, bare(raw), bare(seen)); }
  return [...new Set(out.map(spaced))].filter(Boolean);
}

export function matches(vars, quote) {
  const q = spaced(quote);
  if (!q) return false;
  if (q.replace(/ /g, "").length >= SHORT) { const sq = q.replace(/ /g, ""); return vars.some((x) => x.replace(/ /g, "").includes(sq)); }
  const word = new RegExp(`(^|[^\\p{L}\\p{N}])${q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}($|[^\\p{L}\\p{N}])`, "u");
  return vars.some((x) => word.test(x));
}

// "Reply" takes a row's whole text: a row that is exactly the quote is its source; file names are matched only this way
const exact = (vars, quote) => { const q = spaced(quote); return !!q && (vars.includes(q) || !!vars.names?.includes(q)); };

// rows a quote can come from: a session's reply, another session's reply, a file it sent, the owner's own words, his answer to a card
export const quotableRow = (x) => !x.error && (x.kind === "out" || x.kind === "reply" || x.kind === "answer" || x.kind === "file"
  || (x.kind === "in" && !x.sender && !x.to));

// the names of a message's files: "Reply" on a message with only files quotes them (feed.js); a screen recording — its other files
export const fileNames = (files) => (files || []).map((f) => f.name).filter(Boolean).join(", ");
const shownFiles = (x) => (!x.label && castOf(x)?.files) || x.attachments;

// what a row is quoted by: its text, what is on screen (a label), what was chosen on a card (what), a file's caption; and, as a
// whole quote only (names), a file's name and the names of the owner's files — the quotes "Reply" puts on them: a word picked
// from a sentence ("report") does not lead to a file named "report.pdf"
export function quotedBy(x, what = "") {
  const v = variants(x.text, x.label, what, x.kind === "file" ? x.caption : "");
  v.names = variants(x.kind === "file" ? x.name : "", x.kind === "in" ? fileNames(shownFiles(x)) : "");
  return v;
}

// the newest earlier row that is exactly the quote, else the newest earlier one that contains it; varsOf(x) — its variants.
// A session repeating the owner's words in a longer reply after him does not take the jump from his own bubble
export function quoteSource(rows, msg, varsOf) {
  const pool = rows.filter((x) => quotableRow(x) && x.seq < msg.seq);
  for (const test of [exact, matches]) {
    for (let i = pool.length - 1; i >= 0; i--) if (test(varsOf(pool[i]), msg.quote)) return pool[i];
  }
  return null;
}
