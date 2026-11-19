// "Hide/Show" questions: one client-side state per session for the cards in the feed (dock.js) and the rows in the
// "Waiting for you" strip (strip.js). Available only while there are 2+ open questions; the core and localStorage don't need it.
import { btn } from "./dom.js";
import { render } from "./state.js";
import { t, CLOSED } from "./i18n.js";

const folded = new Set();   // names of sessions with hidden questions

// questions are AskUserQuestion and those shown to the hub; a permission or tool-connection request is not a question:
// it blocks the turn, so it is never hidden
export const openAsks = (s) => (s.pending || []).filter((q) => q.tool === "AskUserQuestion").length + (s.shown || []).length;
export const foldable = (s) => openAsks(s) >= 2;
// fewer than two questions left — "Hide" is cleared: otherwise the next pair of questions would appear already hidden
export function isFolded(s) {
  if (!foldable(s)) { folded.delete(s.name); return false; }
  return folded.has(s.name);
}

// toggle in the top-right corner of the "Waiting for you" strip (strip.js): an ordinary small button, like "To the question"
export function foldToggle(s) {
  const hidden = isFolded(s);
  const b = btn(`${hidden ? t("web.fold.show") : t("web.fold.hide")} ${hidden ? CLOSED : "▾"}`,
    () => { if (hidden) folded.delete(s.name); else folded.add(s.name); render(); });
  b.classList.add("fold-toggle-btn");   // red border matching the "Waiting for you" strip (dock.css)
  return b;
}
