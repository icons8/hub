// reply to selected text: select a piece of a single reply in the feed and a bar appears over the selection with
// "Reply" · reactions · "Copy". The feed puts what can be done with a row on its node (row.__quote: reply is the target
// of the "Reply" button, react is the row number for a reaction). The bar stands above the first line of the selection, across
// from the point where the mouse button was released (with the keyboard — from the end of the selection), inside the
// column of the message; no room above (the head, a sticky strip) — under the last line: it does not cover the selected
// words. With a finger the OS system
// menu already shows over the selection, so the same actions go in a strip above the input field; the mode depends on
// what made the selection, not on the window width (a narrow window with a mouse still gets the bar). The feed already
// keeps the selection across re-renders (state.js renderLive) — the bar does not touch it:
// pressing in it does not clear the selection. A triple click selects the whole message, not one paragraph. Outside the
// feed a plugin or a module marks the text of one message with data-hub-message (Plugin API 1.39): a triple click there
// selects all of it too, and a selected piece of it gets the bar with "Copy" only
import { $, el, icon } from "./dom.js";
import { render } from "./state.js";
import { banner } from "./api.js";
import { copyText } from "./markdown.js";
import { REACTIONS, emojiBtn, react, reactOn, replyTo } from "./feed.js";
import { t } from "./i18n.js";

const GAP = 8;
const LIFT = 6;   // between the bar and the selected words
// touch — the last press was a finger or a pen (before the first one, by device type); at — where the mouse was
// released: while the selection stays the same, the bar stays there
// moved — the selection changed while the mouse was held (a stray click or the scrollbar is not a selection: the point is not its)
// clicks — the click count of the last mouse press: a double click selects a word, the bar waits for a selection made on purpose
// down — where the last mouse press was: a triple press dragged on is the browser's selection by paragraphs, not the whole message
// word — what that double click selected: dragging on from it grows a phrase, and the phrase gets the bar
let bar = null, pointer = false, moved = false, timer = 0, at = null, clicks = 0, word = null, down = null, touch = matchMedia("(pointer: coarse)").matches;
let shown = null;   // the message box the bar stands at

const MARKED = "[data-hub-message]";
const SKIP = "button, input, textarea, select, audio, video, [hidden], .sr-only";
const elOf = (n) => (n?.nodeType === 1 ? n : n?.parentElement);
const rowOf = (n) => elOf(n)?.closest(".feed .row");
// the text of a feed row itself, not of a row nested in it
const boxOf = (row) => [...row.querySelectorAll(".md, .summary, .own-text")].find((m) => m.parentElement.closest(".row") === row);

// the whole message a node is in: a marked one, or the text of a feed row; null — not in a message's text
function messageBox(n) {
  const marked = elOf(n)?.closest(MARKED);
  if (marked) return marked;
  const row = rowOf(n), box = row && boxOf(row);
  return box?.contains(n) ? box : null;
}

// a selection inside a single feed row that has "Reply" — returns its actions and the selected message text: edges that
// went past the text (time, buttons, quote; a triple click ends the selection on the next block) are clipped to it; otherwise null
// copy is the text for "Copy": with the line breaks between paragraphs when the selection is all inside the text
function picked() {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  const marked = elOf(range.startContainer)?.closest(MARKED);
  if (marked) {
    const f = document.activeElement;   // the words in a field of the message (editing it) are the field's
    if (!marked.contains(range.endContainer) || (marked.contains(f) && f.matches("input, textarea"))) return null;
    const text = sel.toString().trim();
    return text ? { box: marked, range, text, copy: text } : null;
  }
  const row = rowOf(range.startContainer);
  if (!row?.__quote || row !== rowOf(range.endContainer)) return null;
  const box = boxOf(row);
  if (!box || !range.intersectsNode(box)) return null;
  const r = range.cloneRange(), inside = box.contains(r.startContainer) && box.contains(r.endContainer);
  if (!box.contains(r.startContainer)) r.setStart(box, 0);
  if (!box.contains(r.endContainer)) r.setEnd(box, box.childNodes.length);
  const text = r.toString().trim();
  return text ? { row, box, range: r, text, copy: (inside && sel.toString().trim()) || text, ...row.__quote } : null;
}

// a triple click: the whole message from its first visible word to its last (buttons and hidden parts left out)
function selectWhole(box) {
  const walk = document.createTreeWalker(box, NodeFilter.SHOW_TEXT), nodes = [];
  while (walk.nextNode()) {
    const n = walk.currentNode;
    if (n.data.trim() && !n.parentElement.closest(SKIP) && n.parentElement.getClientRects().length) nodes.push(n);
  }
  if (!nodes.length) return;
  const r = document.createRange();
  r.setStart(nodes[0], 0);
  r.setEnd(nodes.at(-1), nodes.at(-1).length);
  const sel = document.getSelection();
  sel.removeAllRanges();
  sel.addRange(r);
}

function hide() {
  if (bar) bar.hidden = true;
  shown = null;
}

function build(p) {
  if (!bar) {
    bar = el("div", "quote-bar");
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", t("web.quote.bar"));
    bar.hidden = true;
    // pressing in the bar does not clear the selection (mouse); with a finger the text was already taken when shown
    bar.addEventListener("mousedown", (e) => e.preventDefault());
    document.body.append(bar);
  }
  const kids = [];
  if (p.reply) {
    const reply = el("button", "linkbtn qb-reply");
    reply.type = "button";
    reply.append(icon("reply"), document.createTextNode(t("web.feed.reply")));
    reply.addEventListener("click", () => {
      hide();
      replyTo({ ...p.reply, quote: p.copy });   // with its line breaks: blocks of a summary do not run together
    });
    kids.push(reply);
  }
  if (p.react && reactOn()) {
    const box = el("span", "qb-reactions");
    for (const e of REACTIONS) {
      const b = emojiBtn("react-pick qb-react", e, t("web.feed.react_pick", { emoji: e }));
      b.addEventListener("click", async () => {
        hide();
        document.getSelection()?.removeAllRanges();   // the piece has done its job, the reaction failing too (render() does not come then)
        if (await react(p.react.session, p.react.target, e, p.text, true)) render();
      });
      box.append(b);
    }
    kids.push(el("span", "qb-sep"), box);
  }
  const copy = el("button", "linkbtn qb-copy");
  copy.type = "button";
  copy.title = t("web.quote.copy");
  copy.setAttribute("aria-label", t("web.quote.copy"));
  copy.append(icon("copy"));
  copy.addEventListener("click", () => {
    copyText(p.copy).catch(() => banner(t("web.quote.copy_failed")));
    hide();
  });
  if (kids.length) kids.push(el("span", "qb-sep"));
  kids.push(copy);
  bar.replaceChildren(...kids);
}

function place(p) {
  const strip = touch;
  bar.classList.toggle("strip", strip);
  bar.hidden = false;
  bar.style.left = bar.style.top = bar.style.width = "";
  const w = bar.offsetWidth, h = bar.offsetHeight;
  if (strip) {
    // as a strip above the input field of the open session; no field — at the bottom edge
    const c = $("msg")?.closest(".composer")?.getBoundingClientRect();
    const left = c ? c.left : GAP, width = c ? c.width : innerWidth - 2 * GAP;
    bar.style.left = `${left}px`;
    bar.style.width = `${width}px`;
    bar.style.top = `${(c ? c.top : innerHeight) - GAP - bar.offsetHeight}px`;
    return;
  }
  // across from the cursor (from the keyboard — the end of the selection), inside the column the message scrolls in
  let x, caret = null;
  if (at) x = at.x;
  else {
    // at the caret: when selecting backwards (Shift+↑) it is at the start
    const sel = document.getSelection(), end = p.range.cloneRange();
    if (p.box.contains(sel.focusNode)) end.setStart(sel.focusNode, sel.focusOffset);
    end.collapse(true);
    caret = [...end.getClientRects()].pop() || [...p.range.getClientRects()].pop() || p.range.getBoundingClientRect();
    x = getComputedStyle(p.box).direction === "rtl" ? caret.left : caret.right;
  }
  const col = columnOf(p.box), c = col?.getBoundingClientRect();
  const lo = Math.max(GAP, c ? c.left : 0), hi = Math.min(innerWidth - GAP, c ? c.right : innerWidth) - w;
  const left = Math.max(lo, Math.min(x - w / 2, hi));
  // a column narrower than the bar: only its own width is looked at
  const xs = [0, 0.5, 1].map((a) => Math.max(lo, Math.min(left + 1 + a * (w - 2), hi + w - 1)));
  // above the first line of the selection; no room there — under the last one; neither (the selection fills the column) — as before, at the cursor
  const lines = [...p.range.getClientRects()].filter((r) => r.width && r.height);
  if (!lines.length) lines.push(p.range.getBoundingClientRect());
  const first = Math.min(...lines.map((r) => r.top)), last = Math.max(...lines.map((r) => r.bottom));
  const room = (top) => xs.every((px) => [top + 1, top + h / 2, top + h - 1].every((py) => clear(col, px, py)));
  let top = [first - LIFT - h, last + LIFT].find(room);
  if (top === undefined) {
    const y = at ? at.y : caret.top;
    const up = Math.max(GAP, c ? c.top + GAP : 0), down = Math.min(innerHeight, c ? c.bottom : innerHeight) - GAP - h;
    top = Math.max(up, Math.min(y - GAP - h, down));
  }
  bar.style.left = `${left}px`;
  bar.style.top = `${top}px`;
}

// what the message scrolls in (the feed, a plugin's list): the bar stays inside it; null — the window
function columnOf(box) {
  // overflow-x: auto alone computes overflow-y to auto as well: such a box (a wide table) is not a column unless it scrolls down
  for (let e = box.parentElement; e && e !== document.body; e = e.parentElement)
    if (/auto|scroll/.test(getComputedStyle(e).overflowY) && (e.matches(".feed") || e.scrollHeight > e.clientHeight)) return e;
  return null;
}

// nothing stands over this point of the column: not the head above it, a sticky strip in it, the input field or the window edge
function clear(col, x, y) {
  if (x < 0 || y < 0 || x >= innerWidth || y >= innerHeight) return false;
  if (!col) return true;
  const hit = document.elementsFromPoint(x, y).find((e) => !bar.contains(e));
  if (!hit || !col.contains(hit)) return false;
  for (let e = hit; e !== col; e = e.parentElement) if (/sticky|fixed/.test(getComputedStyle(e).position)) return false;
  return true;
}

function check() {
  const p = picked();
  if (!p || (clicks === 2 && !touch && document.getSelection().toString() === word)) return hide();
  build(p);
  place(p);
  shown = p.box;
}

// mouse — after release (not while dragging); finger and keyboard — once the selection has settled
document.addEventListener("pointerdown", (e) => {
  if (bar?.contains(e.target)) return;
  pointer = e.pointerType === "mouse";
  touch = !pointer;
  at = null;
  moved = false;
  down = { x: e.clientX, y: e.clientY };
  if (pointer) hide();   // finger: the selection survives scrolling — a touch clears it, and selectionchange decides
}, true);
document.addEventListener("pointerup", (e) => {
  if (!pointer) return;
  pointer = false;
  at = moved ? { x: e.clientX, y: e.clientY } : null;
  clearTimeout(timer);
  timer = setTimeout(check, 0);
}, true);
document.addEventListener("pointercancel", () => { pointer = false; }, true);
document.addEventListener("mousedown", (e) => {
  if (bar?.contains(e.target)) return;
  clicks = e.detail;
  word = null;
  if (clicks === 2) setTimeout(() => { word = document.getSelection()?.toString() ?? null; }, 0);   // the browser selects it after the press
}, true);
document.addEventListener("keydown", (e) => { if (e.shiftKey) clicks = 0; });   // a word grown with Shift+arrows — a selection made on purpose
document.addEventListener("click", (e) => {
  if (e.detail !== 3 || elOf(e.target)?.closest(`a, ${SKIP}, [contenteditable]:not([contenteditable="false"])`)) return;
  // not the "moved" flag: the triple press itself changes the selection to its paragraph while the button is held
  if (down && Math.hypot(e.clientX - down.x, e.clientY - down.y) > 4) return;
  const box = messageBox(e.target);
  if (box) selectWhole(box);
});
document.addEventListener("selectionchange", () => {
  if (pointer) { moved = true; return; }
  clearTimeout(timer);
  timer = setTimeout(check, touch ? 350 : 200);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { at = null; if (bar && !bar.hidden) { hide(); e.preventDefault(); } return; }   // one Esc — one thing: not the quote or the turn too
  if (e.shiftKey || e.metaKey || e.ctrlKey) at = null;   // selecting with the keyboard (Shift+arrows, select all) — the bar goes at the end of the selection
});
// scrolling the feed, the page or a box holding the message (Inbox, To-do) moves the selection out from under the bar; the phone strip is not tied to the selection — it stays
document.addEventListener("scroll", (e) => {
  if (bar && !bar.hidden && !bar.classList.contains("strip")
      && (e.target === document || (e.target.nodeType === 1 && e.target.contains(shown)))) { at = null; hide(); }
}, true);
// phone keyboard, rotation, a different window width — move to a new place (the cursor is no longer there — go to the end of the selection)
addEventListener("resize", () => { if (bar && !bar.hidden) { at = null; check(); } });
