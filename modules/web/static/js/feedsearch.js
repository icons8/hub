// Search over the text of the open session's feed: a row under the feed header, opened with the magnifier in the
// header or ⌘F/Ctrl+F. No model involved. First the loaded part (what the page already holds), searched by data, not
// by DOM: collapsed "Brief" and "more" content is found too; then, on ↑ past the earliest hit or "Search earlier", the
// past on the server (/api/history/search: the core's live feed older than what is loaded, then the journal archives,
// in batches). Which fields of a row to search comes from the core's list (snapshot: search_fields, core/views.py
// SEARCH_FIELDS); comparison is case-insensitive and folds the Cyrillic yo into ye, as in the core.
// Fast: typing does not redraw the feed — counting works on data with a per-row text cache, highlighting touches only
// the matching nodes and no more than PAINT; the feed is rebuilt only by a jump to a match that has to be expanded. A
// match older than the loaded part is shown as an excerpt around it.
import { el, icon, dayTime, OWN } from "./dom.js";
import { S, render, setScroll } from "./state.js";
import { t } from "./i18n.js";
import { keyOf } from "./feed.js";
import { fieldMic } from "./dictate.js";

const DELAY = 150;      // ms after the last keystroke — count and highlight
const PAINT = 300;      // occurrences marked at once (in the feed window and nearby); the rest are only counted
const LIMIT = 1000;     // more matches than this — "refine the query", the archive is not searched further
const FALLBACK = ["label|text"];   // old server (snapshot without search_fields): the row's text only
const TAB = Math.random().toString(36).slice(2, 10);   // the tab: its new search cancels the previous one on the server

export const fold = (s) => s.toLowerCase().replaceAll("\u0451", "\u0435");   // i18n-ok: letters, not text

let cb = null;   // from main.js: items(name) — what is loaded, in feed order, top(name) — the number of the earliest,
                 // show(name, it) — go to a row (expand it and put it mid-window), excerpt(name, it) / back(name) /
                 // inExcerpt(name) — the excerpt
export function setup(callbacks) { cb = callbacks; }

const fs = {
  open: false, name: null, q: "", needle: "",
  loaded: [],      // matches in the loaded part: feed records, in feed order
  arch: [],        // found by the server before the loaded part: {key, seq, ts, kind, id, snippet}, by number
  at: null,        // the current one: the row key
  shown: null,     // the match already jumped to (or visible highlighted): if the current one differs, Enter goes to it first
  first: null,     // the earliest loaded record (for "in the loaded part — since …")
  opened: new Set(),   // steps and long messages expanded by a jump: closing the search collapses them back
  srv: null,       // server search: {state: idle|run|done|capped|building|unparsed|failed|old|off, cursor, before, left, ctl, unparsed}
  timer: 0,
  since: 0,        // number of the last event when the query was typed: a newer own message is not a match
};
let bar = null, input = null, count = null, status = null;

// ---- what is searched in a row ----
const texts = new WeakMap();   // record -> its search strings (fold): typing does not iterate over the fields again
function strings(v, out) {
  if (typeof v === "string") out.push(v);
  else if (Array.isArray(v)) for (const x of v) strings(x, out);
  else if (v && typeof v === "object") for (const x of Object.values(v)) strings(x, out);
  return out;
}
function folded(it) {
  let v = texts.get(it);
  if (v) return v;
  const fields = (S.snap.search_fields || {})[it.kind] || (S.snap.search_fields ? [] : FALLBACK);
  v = [];
  for (const f of fields) {
    const name = f.split("|").find((n) => it[n]);
    if (name) for (const s of strings(it[name], [])) v.push(fold(s));
  }
  texts.set(it, v);
  return v;
}
export const matches = (it, needle) => !!needle && folded(it).some((s) => s.includes(needle));
// your own message sent while the search is open (you typed in the input field what you are searching for) is not a
// find: no count, no highlight. New session rows are searched; a new query will find your own too — counting starts
// from the moment the query is typed; the core's retelling of your answer to a card (on_behalf) is yours too
const sentSince = (it) => it.kind === "in" && (it.from?.kind === "owner" || it.on_behalf === "owner") && it.seq > fs.since;

// ---- matches: the archive (older than everything loaded) by number, then the loaded part in feed order ----
const all = () => [...fs.arch, ...fs.loaded.map((it) => ({ key: keyOf(it), item: it }))];
function recount(items = cb.items(fs.name)) {
  fs.first = items[0] || null;
  fs.loaded = fs.needle ? items.filter((it) => matches(it, fs.needle) && !sentSince(it)) : [];
  const keys = new Set(fs.loaded.map(keyOf));
  fs.arch = fs.arch.filter((h) => !keys.has(h.key));   // loaded by scrolling — now it is in the loaded part
  const list = all();
  if (!list.some((h) => h.key === fs.at)) fs.at = list.length ? list[list.length - 1].key : null;   // the newest
}

// ---- the search row ----
function build() {
  bar = el("div", "fsearch");
  bar.setAttribute("role", "search");
  const row = el("div", "fs-row");
  input = el("input");
  input.type = "text";   // not "search": the browser's own "×" button next to our "close" would confuse
  input.placeholder = t("web.search.placeholder");
  input.setAttribute("aria-label", t("web.search.placeholder"));
  input.autocomplete = "off";
  input.spellcheck = false;
  count = el("span", "fs-n");
  count.setAttribute("aria-live", "polite");
  const btn = (name, title, fn) => {
    const b = el("button", "btn quiet fs-btn");
    b.type = "button";
    b.title = title;
    b.setAttribute("aria-label", title);
    b.append(typeof name === "string" ? document.createTextNode(name) : name);
    b.addEventListener("click", fn);
    return b;
  };
  const mic = fieldMic(input, { space: false });
  // a phone closes the search with the word, not the cross (the other one is hidden there by CSS)
  const word = btn(t("web.composer.cancel"), t("web.search.close"), () => close());
  word.classList.add("fs-cancel");
  const cross = btn(icon("close"), t("web.search.close"), () => close());
  cross.classList.add("fs-close");
  row.append(icon("search"), input, mic, count, btn("↑", t("web.search.earlier"), () => step(-1)),
    btn("↓", t("web.search.later"), () => step(1)), cross, word);
  status = el("div", "fs-status");
  bar.append(row, mic.hubNote, status);   // a failed dictation: the reason and the kept recording, under the row
  input.addEventListener("input", () => {
    clearTimeout(fs.timer);
    fs.timer = setTimeout(() => setQuery(input.value), DELAY);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); return; }   // the search only: neither a question nor a turn
    if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      clearTimeout(fs.timer);
      if (input.value !== fs.q) setQuery(input.value);
      step(e.shiftKey ? 1 : -1);   // Enter — earlier (we search backwards from the newest), Shift+Enter — later
    }
  });
  // the row's height is the feed's offset below it (it lies over the top of the feed, in the feed's grid row)
  new ResizeObserver(() => bar.parentElement?.style.setProperty("--fs-h", `${bar.offsetHeight}px`)).observe(bar);
}

export const isOpen = (name) => fs.open && fs.name === name;
export function open(name) {
  if (!bar) build();
  if (!isOpen(name)) {
    if (fs.open) close(true);   // another session's search — close it properly (collapse what it expanded)
    reset();
    fs.open = true;
    fs.name = name;
    input.value = "";
  }
  place();
  input.focus();
  input.select();
  draw();
}
export function close(quiet = false) {
  if (!fs.open) return;
  const name = fs.name;
  for (const k of fs.opened) (k.startsWith("t:") ? S.expanded : S.openSteps).delete(k.slice(2));
  fs.opened.clear();
  reset();
  fs.open = false;
  bar?.remove();
  cb.back(name, quiet);   // from the excerpt back to the feed; the marks go away with the next redraw (painted)
  if (!quiet) render();
}
function reset() {
  clearTimeout(fs.timer);
  fs.srv?.ctl?.abort();
  Object.assign(fs, { q: "", needle: "", loaded: [], arch: [], at: null, shown: null, srv: null });
}
export const toggle = (name) => (isOpen(name) ? close() : open(name));
// the row lives in main, next to the feed; the header is recreated on every event, the row is not (focus and typed text survive)
function place() {
  const main = document.querySelector("main");
  if (!main || !bar) return;
  if (bar.parentElement !== main) main.append(bar);
}

function setQuery(q) {
  if (q === fs.q) return;
  const was = fs.srv;
  fs.q = q;
  fs.needle = fold(q.trim());
  fs.since = S.snap.seq || 0;
  was?.ctl?.abort();
  fs.srv = null;
  fs.arch = [];
  fs.at = fs.shown = null;
  if (cb.inExcerpt(fs.name)) cb.back(fs.name);   // new query — the previous excerpt is not needed: back to the feed (the redraw
  else {                                          // will count and highlight by itself — painted)
    recount();
    paint(document.querySelector("main .feed"));
  }
  fs.shown = document.querySelector("main .feed mark.fs-cur") ? fs.at : null;
  scrollToCur();
  draw();
}

// ---- jumping ----
async function step(d) {
  if (!fs.needle) return;
  const list = all();
  let i = list.findIndex((h) => h.key === fs.at);
  // the current one is not on screen yet (typed, and it is in collapsed steps or in the past) — the first press shows it
  if (i >= 0 && fs.shown !== fs.at) { await goTo(list[i]); return; }
  if (i < 0) i = list.length;
  const j = i + d;
  if (j < 0) {   // before the earliest found — search further into the past; when the answer comes we jump to it
    if (fs.srv?.state === "run" || fs.srv?.state === "building") fs.srv.jump = true;
    else if (!fs.srv || RESUME.includes(fs.srv.state)) searchOlder(true);
    return;
  }
  if (j >= list.length) return;
  await goTo(list[j]);
}
async function goTo(h) {
  fs.at = fs.shown = h.key;
  draw();
  // the row is already in the feed and the match in it is visible (not in a collapsed "more", not in a closed step) —
  // move the highlight and scroll, without rebuilding the feed: with hundreds of rows that costs hundreds of ms
  const f = document.querySelector("main .feed");
  if (h.item && !cb.inExcerpt(fs.name) && f) {
    paint(f);
    const m = f.querySelector("mark.fs-cur"), c = m?.closest(".clamp, .clamp-2");
    const hidden = !m || !m.getClientRects().length || (c && m.getBoundingClientRect().bottom > c.getBoundingClientRect().bottom);
    if (!hidden) { scrollToCur(); return; }
  }
  if (h.item) { cb.back(fs.name, true); remember(h.item); cb.show(fs.name, h.item); }
  else { remember(h); await cb.excerpt(fs.name, h); }
  scrollToCur();
}
// what the jump will expand (cb.show: a step's output, a long message) — remember it if it was collapsed
function remember(it) {
  if (it.kind === "tool" && it.id && !S.expanded.has(it.id)) fs.opened.add("t:" + it.id);
  const m = "m" + (it.id ?? it.seq);
  if (!S.openSteps.has(m)) fs.opened.add("s:" + m);
  // a task's frame for the model is folded (js/feed.js svcFold): the text of a hit in it is not on the page until opened
  const k = "s" + (it.id ?? it.seq);
  if (it.task && it.brief && !(S.compact && S.brief.task) && !S.openSteps.has(k)) { S.openSteps.add(k); fs.opened.add("s:" + k); }
}
function scrollToCur() {
  const m = document.querySelector("main .feed mark.fs-cur");
  if (!m) return;
  const f = m.closest(".feed"), r = m.getBoundingClientRect(), top = f.getBoundingClientRect().top;
  if (r.top < top + 8 || r.bottom > top + f.clientHeight - 8) {
    f.__pinned = false;   // the feed does not stick to the bottom while we look at a find (like the owner scrolling up)
    cb.moved?.(fs.name);
    setScroll(f, f.scrollTop + r.top - top - f.clientHeight / 2);
  }
}

// ---- the past on the server ----
const RESUME = ["idle", "failed", "building", "stopped"];   // from these — continue from the same place
async function searchOlder(jump) {
  const name = fs.name, q = fs.q.trim();
  if (q.length < 2) return;
  const srv = fs.srv && RESUME.includes(fs.srv.state) ? fs.srv : (fs.srv = { state: "idle", cursor: null, before: cb.top(name) });
  srv.state = "run";
  srv.jump = jump || srv.jump;
  draw();
  while (fs.srv === srv && srv.state === "run") {
    srv.ctl = new AbortController();
    const url = `/api/history/search?session=${encodeURIComponent(name)}&q=${encodeURIComponent(q)}&before=${srv.before}`
      + (srv.cursor ? `&cursor=${encodeURIComponent(srv.cursor)}` : "") + `&tab=${TAB}`;
    let r = null, code = 0;
    try {
      const res = await fetch(url, { signal: srv.ctl.signal });
      code = res.status;
      r = res.ok ? await res.json() : null;
    } catch { /* cancelled or network */ }
    if (fs.srv !== srv || srv.state !== "run") return;   // the query changed, the search was closed or stopped with "stop"
    if (!r) { srv.state = code === 404 ? "old" : "failed"; draw(); return; }
    if (r.cancelled) continue;
    const have = new Set(all().map((h) => h.key));
    const fresh = (r.hits || []).filter((h) => !have.has(h.key));
    fs.arch = fresh.concat(fs.arch).sort((a, b) => a.seq - b.seq);
    srv.cursor = r.cursor;
    srv.left = r.left;
    if (r.unavailable) srv.state = "off";
    else if (r.failed) srv.state = "failed";
    else if (r.unparsed) { srv.state = "unparsed"; srv.unparsed = r.unparsed; }
    else if (r.capped || all().length >= LIMIT) srv.state = "capped";
    else if (r.done) srv.state = "done";
    else if (r.building) {   // an archive on the way is still being parsed — same cursor again in 3 s
      srv.state = "building";
      setTimeout(() => { if (fs.srv === srv && srv.state === "building") searchOlder(false); }, 3000);
    }
    const list = all(), at = list.findIndex((h) => h.key === fs.at);
    if (srv.jump && (at > 0 || (at < 0 && list.length))) {   // ↑ was waiting — go to the nearest one before the current
      srv.jump = false;
      draw();
      await goTo(list[at > 0 ? at - 1 : list.length - 1]);
    }
    draw();
  }
}
function stop() {
  if (!["run", "building"].includes(fs.srv?.state)) return;
  fs.srv.ctl?.abort();
  fs.srv.state = "stopped";
  draw();
}

// ---- view ----
function draw() {
  if (!bar) return;
  const list = all(), i = list.findIndex((h) => h.key === fs.at);
  // on a phone the count takes only its text, so a new count resizes the field after the browser scrolled it to the caret:
  // the end of a long query slid under the microphone. A caret at the end is brought back into view on the next frame
  const tail = document.activeElement === input && input.selectionEnd === input.value.length;
  count.textContent = !fs.needle ? "" : list.length ? t("web.search.count", { n: list.length - i, total: list.length >= LIMIT ? `${LIMIT}+` : list.length })
    : t("web.search.none");
  if (tail) requestAnimationFrame(() => { input.scrollLeft = input.scrollWidth; });
  const srv = fs.srv, more = fs.q.trim().length >= 2;
  const link = (text, fn) => { const a = el("button", "linkbtn", text); a.type = "button"; a.addEventListener("click", fn); return a; };
  const parts = [];
  if (!srv || srv.state === "idle") {
    if (fs.first) parts.push(t("web.search.loaded_since", { when: dayTime(fs.first.ts) }));
    if (more) parts.push(link(t("web.search.search_older"), () => searchOlder(false)));
  } else if (srv.state === "run" || srv.state === "building") {
    parts.push(srv.state === "building" ? t("web.search.archive_building") : t("web.search.searching", { left: srv.left ?? "…" }),
      link(t("web.search.stop"), stop));
  } else if (srv.state === "done") parts.push(t("web.search.older_done", { count: fs.arch.length }));
  else if (srv.state === "stopped") parts.push(t("web.search.stopped", { count: fs.arch.length }), link(t("web.search.resume"), () => searchOlder(false)));
  else if (srv.state === "capped") parts.push(t("web.search.capped", { limit: LIMIT }));
  else if (srv.state === "unparsed") parts.push(t("web.search.unparsed", { count: fs.arch.length }));
  else if (srv.state === "failed") parts.push(t("web.search.failed"), link(t("web.search.retry"), () => searchOlder(false)));
  else if (srv.state === "old") parts.push(t("web.search.old_server"));
  else if (srv.state === "off") parts.push(t("web.search.unavailable"));
  status.replaceChildren(...parts.flatMap((p, k) => (k ? [document.createTextNode(" · "), p] : [p])));
  status.hidden = !fs.needle;
}

// ---- highlighting: after every feed build (main.js), after typing and scrolling ----
// only rows in the feed window and one screen above and below (and the current one): with a thousand loaded rows and a
// common letter, marking all occurrences means thousands of nodes and hundreds of ms; the rest are highlighted when
// scrolled to
export function painted(f, name, items) {
  if (!isOpen(name)) {
    if (fs.open) close(true);   // left the session (or went to a board) — the search is closed
    unmark(f);   // the feed keeps its unchanged rows between redraws (main.js mergeFeed): the marks are taken off here
    return;
  }
  place();
  recount(items);
  draw();
  paint(f);
}
let marked = false;   // marks are on the page: a feed with no search is not walked for them on every redraw
function unmark(f) {
  if (!marked) return;
  marked = false;
  const parents = new Set();
  for (const m of f?.querySelectorAll("mark.fs-hit") || []) { parents.add(m.parentNode); m.replaceWith(...m.childNodes); }
  for (const p of parents) p.normalize();   // text pieces become one node again: otherwise a new needle spanning their edge would not be found
}
function paint(f) {
  if (!f) return;
  unmark(f);
  if (!f.__fsScroll) {   // on scroll — highlight what came into the window (the feed node lives across redraws)
    f.__fsScroll = true;
    let timer = 0;
    f.addEventListener("scroll", () => { clearTimeout(timer); timer = setTimeout(() => { if (fs.needle) paintVisible(f); }, 100); }, { passive: true });
  }
  if (fs.needle) paintVisible(f);
}
function paintVisible(f) {
  const keys = new Set(all().map((h) => h.key)), box = f.getBoundingClientRect(), h = box.height;
  // all measurements first, then marking: a measurement after a DOM edit means a new layout of the whole feed (~50 ms for 600 rows)
  const rows = [...f.querySelectorAll(`.feed-inner [data-key]${OWN}`)].filter((row) => keys.has(row.dataset.key) && !row.querySelector("mark.fs-hit"))
    .filter((row) => { const r = row.getBoundingClientRect(); return row.dataset.key === fs.at || (r.bottom >= box.top - h && r.top <= box.bottom + h); });
  let n = 0;
  for (const row of rows) {
    const cur = row.dataset.key === fs.at;
    if (cur || n < PAINT) n += markIn(row, fs.needle, cur);
  }
}
// mark the occurrences in a feed row; if cur, the first of them is the current one. Returns how many were marked
function markIn(root, needle, cur) {
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (x) => (x.parentElement?.closest("button, .fsearch") ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT) });
  const nodes = [];
  while (w.nextNode()) nodes.push(w.currentNode);
  let first = null, count = 0;
  for (const n of nodes) {
    const v = n.nodeValue, low = fold(v);
    if (low.length !== v.length || !low.includes(needle)) continue;   // the length changes under fold (rare letters) — do not mark
    const out = [];
    let i = 0, j;
    while ((j = low.indexOf(needle, i)) >= 0) {
      if (j > i) out.push(document.createTextNode(v.slice(i, j)));
      const m = el("mark", "fs-hit", v.slice(j, j + needle.length));
      marked = true;
      first ||= m;
      count += 1;
      out.push(m);
      i = j + needle.length;
    }
    if (i < v.length) out.push(document.createTextNode(v.slice(i)));
    n.replaceWith(...out);
  }
  if (cur && first) first.classList.add("fs-cur");
  return count;
}

// the feed was trimmed (new rows pushed old ones out of the page's memory): the server search restarts from the new top,
// otherwise the rows between the old top and the new one would fall through
export function trimmed(name) {
  if (!isOpen(name) || !fs.srv) return;
  const was = fs.srv.state;
  fs.srv.ctl?.abort();
  fs.srv = null;
  if (was === "run") searchOlder(false);
}
