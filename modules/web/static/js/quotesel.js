// reply to selected text: select a piece of a single reply in the feed and a bar appears over the selection with
// "Reply" · reactions · "Copy". The feed puts what can be done with a row on its node (row.__quote: reply is the target
// of the "Reply" button, react is the row number for a reaction). With a mouse the bar sits at the point where the
// button was released (close to the cursor), with the keyboard at the end of the selection. With a finger the OS system
// menu already shows over the selection, so the same actions go in a strip above the input field; the mode depends on
// what made the selection, not on the window width (a narrow window with a mouse still gets the bar). The feed already
// keeps the selection across re-renders (state.js renderLive) — the bar does not touch it:
// pressing in it does not clear the selection
import { $, el, icon } from "./dom.js";
import { render } from "./state.js";
import { banner } from "./api.js";
import { copyText } from "./markdown.js";
import { REACTIONS, emojiBtn, react, reactOn, replyTo } from "./feed.js";
import { t } from "./i18n.js";

const GAP = 8;
// touch — the last press was a finger or a pen (before the first one, by device type); at — where the mouse was
// released: while the selection stays the same, the bar stays there
// moved — the selection changed while the mouse was held (a stray click or the scrollbar is not a selection: the point is not its)
let bar = null, pointer = false, moved = false, timer = 0, at = null, touch = matchMedia("(pointer: coarse)").matches;

// a selection inside a single feed row that has "Reply" — returns its actions and the selected message text: edges that
// went past the text (time, buttons, quote; a triple click ends the selection on the next block) are clipped to it; otherwise null
function picked() {
  const sel = document.getSelection();
  if (!sel || sel.isCollapsed || !sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  const rowOf = (n) => (n.nodeType === 1 ? n : n.parentElement)?.closest(".feed .row");
  const row = rowOf(range.startContainer);
  if (!row?.__quote || row !== rowOf(range.endContainer)) return null;
  const box = [...row.querySelectorAll(".md, .summary, .own-text")].find((m) => m.parentElement.closest(".row") === row);
  if (!box || !range.intersectsNode(box)) return null;
  const r = range.cloneRange();
  if (!box.contains(r.startContainer)) r.setStart(box, 0);
  if (!box.contains(r.endContainer)) r.setEnd(box, box.childNodes.length);
  const text = r.toString().trim();
  return text ? { row, box, range: r, text, ...row.__quote } : null;
}

function hide() {
  if (bar) bar.hidden = true;
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
  const reply = el("button", "linkbtn qb-reply");
  reply.type = "button";
  reply.append(icon("reply"), document.createTextNode(t("web.feed.reply")));
  reply.addEventListener("click", () => {
    hide();
    replyTo({ ...p.reply, quote: p.text });
  });
  const kids = [reply];
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
    copyText(p.text).catch(() => banner(t("web.quote.copy_failed")));
    hide();
  });
  kids.push(el("span", "qb-sep"), copy);
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
  // above the cursor (from the keyboard — above the end of the selection), centered on it; no room above — below it; always inside the window
  let x, y, below;
  if (at) [x, y, below] = [at.x, at.y, at.y + 2 * GAP];
  else {
    // at the caret: when selecting backwards (Shift+↑) it is at the start
    const sel = document.getSelection(), end = p.range.cloneRange();
    if (p.box.contains(sel.focusNode)) end.setStart(sel.focusNode, sel.focusOffset);
    end.collapse(true);
    const r = [...end.getClientRects()].pop() || [...p.range.getClientRects()].pop() || p.range.getBoundingClientRect();
    [x, y, below] = [r.right, r.top, r.bottom + GAP];
  }
  let top = y - GAP - h;
  if (top < GAP) top = below;
  top = Math.max(GAP, Math.min(top, innerHeight - h - GAP));
  const left = Math.min(Math.max(GAP, x - w / 2), innerWidth - w - GAP);
  bar.style.left = `${left}px`;
  bar.style.top = `${top}px`;
}

function check() {
  const p = picked();
  if (!p) return hide();
  build(p);
  place(p);
}

// mouse — after release (not while dragging); finger and keyboard — once the selection has settled
document.addEventListener("pointerdown", (e) => {
  if (bar?.contains(e.target)) return;
  pointer = e.pointerType === "mouse";
  touch = !pointer;
  at = null;
  moved = false;
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
document.addEventListener("selectionchange", () => {
  if (pointer) { moved = true; return; }
  clearTimeout(timer);
  timer = setTimeout(check, touch ? 350 : 200);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { at = null; if (bar && !bar.hidden) hide(); return; }
  if (e.shiftKey) at = null;   // selecting with the keyboard — the bar goes at the end of the selection
});
// scrolling the feed moves the selection out from under the bar; the phone strip is not tied to the selection — it stays
document.addEventListener("scroll", (e) => {
  if (bar && !bar.hidden && !bar.classList.contains("strip") && e.target.classList?.contains("feed")) { at = null; hide(); }
}, true);
// phone keyboard, rotation, a different window width — move to a new place (the cursor is no longer there — go to the end of the selection)
addEventListener("resize", () => { if (bar && !bar.hidden) { at = null; check(); } });
