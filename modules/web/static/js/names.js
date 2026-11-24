// Matching a session name to the panel search — the same rule as the native clients (hub-ios SearchNames): the search
// covers names only, from the first letter; a typo is not forgiven.
// - Case, diacritics and width do not matter; "-", "_", ".", "/" and spaces are one word boundary: "hub dev" finds "hub-dev".
// - The start of the name is better than the start of a word, and that is better than the middle of a word; a query of one
//   letter matches only the start of the name or of a word.
// - A query typed in the other keyboard layout ("bru" on the Russian keys) finds the name it would be in the right one; such a match
//   comes after every match of what was typed. The layouts are language data (i18n/data, GET /api/keyboards, setKeyboards);
//   until they come, or from an older server that has no such address, the search matches only what was typed.
// - A query of separators only ("-") is a search with nothing found; spaces only are no search (the field trims them).
// Pure functions: no page, no DOM — sidebar.js uses them, tests/web/names.test.mjs checks them.

const SEP = /[-_./\s]+/u;
export const INNER = 2;   // from this length a query also matches inside a word; shorter — only at the start of the name or a word

// lower case, no diacritics, one space between words
export function fold(s) {
  return String(s ?? "").normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase().split(SEP).filter(Boolean).join(" ");
}

// how well a folded name fits a folded query: 0 — from the start of the name, 1 — from the start of a word, 2 — inside a
// word; null — no match
export function rankFolded(n, q, inner = INNER) {
  if (!q || !n.includes(q)) return null;
  if (n.startsWith(q)) return 0;
  if (n.includes(" " + q)) return 1;
  return [...q].length < inner ? null : 2;
}

export const rank = (name, query, inner = INNER) => rankFolded(fold(name), fold(query), inner);

// the other layouts: for each, what a US key types in it and back; none — the search matches only what was typed
let LAYOUTS = [];
// {layouts: {language: {US key: letter}}} as GET /api/keyboards gives it; anything else — no layouts
export function setKeyboards(data) {
  LAYOUTS = Object.values(data?.layouts ?? {}).filter((keys) => keys && typeof keys === "object").map((keys) => {
    const to = new Map(), back = new Map();
    for (const [k, v] of Object.entries(keys)) {
      if (typeof v !== "string" || [...k].length !== 1 || [...v].length !== 1) continue;
      to.set(k.toLowerCase(), v.toLowerCase());
      if (!back.has(v.toLowerCase())) back.set(v.toLowerCase(), k.toLowerCase());
    }
    return { to, back };
  }).filter((l) => l.to.size);
}

// the same query typed in each other layout, folded: "ghbdtn" → the Russian word on those keys, and back; [] — nothing changes
export function otherLayouts(query) {
  const w = String(query ?? "").toLowerCase();
  const latin = [...w].every((c) => c.codePointAt(0) < 128);
  const out = [];
  for (const { to, back } of LAYOUTS) {
    const s = [...w].map((c) => (latin ? to : back).get(c) ?? c).join("");
    if (s !== w && !out.includes(fold(s))) out.push(fold(s));
  }
  return out;
}
export const otherLayout = (query) => otherLayouts(query)[0] ?? null;

// the panel search: how well any of a session's labels fits the query — 0..2 as `rank` for what was typed, 3..5 for the
// other layout (after every typed match), null — no match. A query that folds to nothing (separators only) finds nothing.
export function matcher(query) {
  const q = fold(query);
  if (!q) return () => null;   // "." is a Russian letter in the other layout, yet a query of separators only finds nothing
  const others = otherLayouts(query).filter((alt) => alt && alt !== q);
  return (labels) => {
    let best = null;
    for (const label of labels) {
      const n = fold(label);
      let r = rankFolded(n, q);
      for (const alt of r === null ? others : []) {
        const o = rankFolded(n, alt);
        if (o !== null && (r === null || 3 + o < r)) r = 3 + o;
      }
      if (r !== null && (best === null || r < best)) best = r;
    }
    return best;
  };
}
