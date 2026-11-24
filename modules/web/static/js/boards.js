// A direction's board inside its session (not in the sidebar: you enter the session and switch to the board from the
// session's own top bar): the direction session header has "Feed | Board"; the board takes the feed's place,
// the input field stays. The board is built in, from the backlog files (GET /api/backlog), with no server per
// direction (a separate `backlog browser` server kept the CPU at ~90%):
// columns are statuses, a card is an id, a title and criteria; a click shows the full description. The board is not
// written to from the page; the owner can pick cards (a circle on a card, a long press with a finger) and ask
// about them in the chat, hand them to executors (a message to the lead) or copy the list — js/mention.js.
// Core facts per card (spec dir-sync, core/board.py): who is working on it, whether it was handed in and reviewed by
// the lead; on the right — the direction's executors without a card. The core never sets Done — facts only hint.
import { el, icon } from "./dom.js";
import { clock } from "./clock.js";
import { render, renderLive, S } from "./state.js";
import { md, copyText } from "./markdown.js";
import { t, locale } from "./i18n.js";
import { awaitDecision, banner, newClientId, sendMessage } from "./api.js";
import { boxes } from "./boxes.js";
import { carry, clear as clearPicks, has as picked, ids as pickedIds, known as knownCards, list as pickedList, listText, set as setPicks, toggle as togglePick } from "./mention.js";

const KEY = "hub.boardOpen";

export function boardOpen(slug) {
  try { return JSON.parse(localStorage.getItem(KEY) || "[]").includes(slug); } catch { return false; }
}

// the link #/board/<slug> (hub direction board) opens the direction session straight on the board
export function markOpen(slug, on = true) {
  let list = [];
  try { list = JSON.parse(localStorage.getItem(KEY) || "[]"); } catch { /* empty */ }
  list = list.filter((x) => x !== slug).concat(on ? [slug] : []);
  try { localStorage.setItem(KEY, JSON.stringify(list)); } catch { /* only until reload */ }
}

function setOpen(slug, on) {
  markOpen(slug, on);
  render();
}

// the two views of a direction session, "Feed" and "Board": the header switch and the same choice in the "More" menu
export function boardChoices(slug) {
  const on = boardOpen(slug);
  return [[false, t("web.boards.feed"), t("web.boards.feed_title")], [true, t("web.boards.board"), t("web.boards.board_title", { slug })]]
    .map(([board, label, title]) => ({ label, title, on: on === board, pick: () => setOpen(slug, board), board }));
}

// "Feed | Board" in the direction session header
export function boardSwitch(slug) {
  const seg = el("div", "seg board-switch fold-seg");   // board-switch tells it from "Brief | Detailed" in any language (clean/clean.js)
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", t("web.boards.switch"));
  for (const c of boardChoices(slug)) {
    const b = el("button", null, c.label);
    b.type = "button";
    b.title = c.title;
    b.setAttribute("aria-pressed", String(c.on));
    b.addEventListener("click", c.pick);
    seg.append(b);
  }
  return seg;
}

// the board in the feed's place. Data is fetched at most once per 10 s per direction; the open card and scroll are kept
const cache = {};   // slug -> {at, data, open: id of the open card}
const REFRESH = 10000;

function load(slug) {
  const c = cache[slug] = cache[slug] || {};
  if (c.loading || (c.at && Date.now() - c.at < REFRESH)) return;
  c.loading = true;
  fetch(`/api/backlog?slug=${encodeURIComponent(slug)}`).then((r) => r.json()).then((data) => {
    Object.assign(c, { at: Date.now(), data, loading: false });
    if (Array.isArray(data?.tasks)) knownCards(slug, data.tasks);   // picked cards: fresh titles, an archived one drops out
    renderLive();   // not an action of the owner: a redraw that comes in the middle of a click on a card must wait for it
  }).catch(() => { c.loading = false; c.at = Date.now(); c.data = c.data || { error: t("web.boards.load_failed") }; renderLive(); });
}

const stamp = (ts) => clock(ts, locale, { day: "2-digit", month: "2-digit" });

// core fact for a card: a line and a subline; no fact — null
function fact(f) {
  if (!f) return null;
  if (f.kind === "working") {
    const sub = [f.gist, f.waiting ? t("web.boards.fact_waiting") : f.stuck ? t("web.boards.fact_stuck") : "",
      f.handed ? t("web.boards.fact_part", { done: f.handed, total: f.total }) : ""].filter(Boolean).join(" · ");
    return { kind: "working", head: t("web.boards.fact_working", { who: f.who.join(", ") }), sub };
  }
  if (f.kind === "unsorted") return { kind: "unsorted", head: t("web.boards.fact_unsorted", { when: stamp(f.since) }),
    sub: t("web.boards.fact_unsorted_who", { who: f.who.join(", ") }) };
  if (f.kind === "unknown") return { kind: "unsorted", head: t("web.boards.fact_unknown", { when: stamp(f.since) }), sub: "" };
  const last = f.last || {};
  const by = last.owner ? t("web.boards.fact_owner") : last.by;
  return { kind: "idle", head: t("web.boards.fact_idle"), sub: last.ts ? t("web.boards.fact_last", { when: stamp(last.ts), by })
    : last.updated ? t("web.boards.fact_last_file", { when: last.updated }) : "" };
}

// ---- picking cards: a circle in the corner of a card turns the mode on; then circles, Shift takes a range, "all" on a column, a bar at the bottom ----
const selecting = new Set();   // directions with the mode on (not kept over a reload; the picks themselves are — js/mention.js)
const bar = {};                // direction -> {step: "run" | null, busy, error}
let anchor = null;             // {slug, id}: where a Shift range starts
let held = false;              // a long press just turned the mode on: the click of the lifted finger lands on a redrawn card — not a pick
const LONG = 500, SLOP = 10;   // a long press with a finger turns the mode on; a finger that moved more is a scroll

function leave(slug) {   // "Cancel" and Esc: the mode and the picks are gone
  selecting.delete(slug);
  bar[slug] = {};
  clearPicks(slug);
  render();
}
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape" || e.defaultPrevented || document.querySelector("dialog[open]") || e.target.closest?.("input, textarea, select, [contenteditable]")) return;
  const open = [...selecting].filter((slug) => document.querySelector(`.boards-body[data-slug="${CSS.escape(slug)}"]`));
  if (!open.length) return;
  e.preventDefault();
  for (const slug of open) leave(slug);
});

// the cards in board order: column after column, top to bottom (what a Shift range runs over)
function order(data) {
  const known = new Set(data.statuses);
  return data.statuses.concat([...new Set(data.tasks.map((x) => x.status))].filter((x) => !known.has(x)))
    .flatMap((st) => data.tasks.filter((x) => x.status === st));
}
const mark = (x) => ({ id: x.id, title: x.title, status: x.status });

// `back`: a press from the keyboard — the redraw gives the focus back to the same circle or card
function pick(slug, task, shift, back) {
  const o = order(cache[slug].data);
  if (shift && anchor?.slug === slug && o.some((x) => x.id === anchor.id)) {
    const a = o.findIndex((x) => x.id === anchor.id), z = o.findIndex((x) => x.id === task.id);
    setPicks(slug, [...pickedList(slug), ...o.slice(Math.min(a, z), Math.max(a, z) + 1).map(mark)]);
  } else togglePick(slug, mark(task));
  anchor = { slug, id: task.id };
  render();
  if (back) [...document.querySelectorAll(`.boards-body[data-slug="${CSS.escape(slug)}"] .board-item`)].find((x) => x.dataset.id === task.id)?.querySelector(back)?.focus();
}

// the circle in the corner of a card: a separate button next to the card (a button inside a button is not reachable for a screen reader);
// outside the mode its first press turns the mode on and picks the card
function circle(slug, task, on) {
  const b = el("button", "pick-box" + (on ? " on" : ""));
  if (on) b.append(icon("allow"));   // the page's own tick (follows a theme's kit), not a text glyph
  b.type = "button";
  b.setAttribute("aria-pressed", String(on));
  b.setAttribute("aria-label", t("web.boards.pick_card", { id: task.id }));
  b.addEventListener("click", (e) => { e.stopPropagation(); selecting.add(slug); pick(slug, task, e.shiftKey, e.detail === 0 ? ".pick-box" : null); });
  return b;
}

function card(slug, t, facts) {
  const c = cache[slug], b = el("button", "board-card" + (c.open === t.id ? " open" : ""));
  b.type = "button";
  const done = t.ac.filter((x) => x.done).length;
  const meta = [t.id, t.ac.length ? `${done}/${t.ac.length}` : "", t.priority || "", ...(t.labels || [])].filter(Boolean);
  const on = picked(slug, t.id);
  b.classList.toggle("picked", on);
  b.append(el("span", "meta", meta.join(" · ")), el("span", "t", t.title));
  const f = fact(facts?.cards?.[t.id]);
  if (f) {
    const box = el("span", "board-fact " + f.kind);
    box.append(el("b", null, f.head));
    if (f.sub) box.append(el("span", null, f.sub));
    b.append(box);
  }
  b.addEventListener("click", () => {   // always opens, in the mode too: only the circle picks (Shift and ⌘ on the card change nothing)
    c.open = c.open === t.id ? null : t.id;
    render();
  });
  // a long press with a finger turns the mode on and picks the card; only an accelerator: the circle in the corner is the way in
  let hold = 0, at = null;
  const stop = () => { clearTimeout(hold); hold = 0; };
  b.addEventListener("pointerdown", (e) => {
    held = false;
    if (e.pointerType !== "touch" || selecting.has(slug)) return;
    at = [e.clientX, e.clientY];
    hold = setTimeout(() => { hold = 0; held = true; selecting.add(slug); pick(slug, t, false); }, LONG);
  });
  b.addEventListener("pointermove", (e) => { if (hold && Math.hypot(e.clientX - at[0], e.clientY - at[1]) > SLOP) stop(); });
  for (const ev of ["pointerup", "pointercancel", "pointerleave"]) b.addEventListener(ev, stop);
  b.addEventListener("contextmenu", (e) => { if (hold || held) e.preventDefault(); });
  b.addEventListener("click", (e) => { if (held) { held = false; e.stopImmediatePropagation(); e.preventDefault(); } }, true);
  const item = el("div", "board-item");
  item.dataset.id = t.id;
  item.append(b, circle(slug, t, on));
  return item;
}

// the line "parallelism X, busy Y: …" for the hand-over bar: from the live snapshot (the lead's own number or the hub's
// shared one, the executors it has now), not a constant
function places(slug, n) {
  const s = (S.snap?.sessions || []).find((x) => x.name === slug) || {};
  const cap = s.parallel ?? S.snap?.parallel;
  if (s.solo_auto || cap === 0) return { off: s.solo_auto ? t("web.boards.run_off_auto", { slug, why: s.solo_auto }) : t("web.boards.run_off", { slug }) };
  if (cap == null) return { text: t("web.boards.run_places_unknown") };
  const live = s.executors_live || 0, now = Math.min(n, Math.max(0, cap - live)), queue = n - now;
  return { text: queue ? t("web.boards.run_places_queue", { cap, live, now, queue }) : t("web.boards.run_places", { cap, live }) };
}

// names the picked cards and forbids the rest: "these cards" in the words was read by a lead as every open card on the board
const runText = (slug) => {
  const ids = pickedIds(slug);
  return t(ids.length > 1 ? "web.boards.run_text_many" : "web.boards.run_text_one", { list: ids.join(", ") });
};

// to the field: the words go into the draft (after what is there), the cards stay as chips, the mode closes so the field is free
function intoField(slug, text) {
  S.drafts[slug] = S.drafts[slug] ? `${S.drafts[slug]}\n${text}` : text;
  boxes.get(slug)?.refresh();
  selecting.delete(slug);
  bar[slug] = {};
  render();
  const ta = document.getElementById("msg");
  ta?.focus();
  ta?.setSelectionRange(ta.value.length, ta.value.length);
}

// the hand-over of a direction in flight or with an unknown outcome: {sig, cid, waiting} by direction, not by the bar (the bar
// is rebuilt by "Reset" and "Cancel"): a repeat of the same hand-over keeps its id — the core takes one message (a second
// one would raise the executors twice); other cards, titles or words — a new one. waiting — in the hub's mailbox, undecided
const handover = new Map();

async function sendRun(slug) {
  const st = bar[slug] = bar[slug] || {};
  if (st.busy || handover.get(slug)?.waiting) return;
  const text = runText(slug), got = carry(slug, text), sent = pickedIds(slug);
  const body = { session: slug, text: got.text ?? text, ...(got.tasks ? { tasks: got.tasks } : {}) };
  const sig = JSON.stringify(body);
  let h = handover.get(slug);
  if (h?.sig !== sig) handover.set(slug, h = { sig, cid: newClientId() });
  st.busy = true; st.error = ""; h.waiting = true;   // on the direction, not the bar: "Reset" rebuilds the bar, not this
  render();
  const res = await sendMessage({ ...body, client_id: h.cid });
  if (res.ok && res.receipt === "queued") return follow(slug, st, h, sent);   // the core has not decided yet: not sent until it does
  st.busy = false; h.waiting = false;
  if (res.ok) return done(slug, sent, "web.boards.run_sent");
  if (res.receipt === "refused") handover.delete(slug);   // the core said no: another try is a new message
  st.error = res.error || t("web.api.rejected");
  render();
}

// the hand-over is in the core: the cards that went are no longer picked (others picked meanwhile stay, with the mode)
function done(slug, sent, say) {
  if (say !== "web.boards.run_stuck") handover.delete(slug);   // stuck keeps its id: a repeat of it is the same message
  setPicks(slug, pickedList(slug).filter((x) => !sent.includes(x.id)));
  bar[slug] = {};
  if (!pickedIds(slug).length) selecting.delete(slug);
  banner(t(say, { slug }));
  render();
}

// the core's reason with a period at its end: the sentence about the picked cards follows it
const dot = (s) => (/[.!?…]$/.test(s.trim()) ? s.trim() : s.trim() + ".");

// "queued" (the hub's mailbox, the core did not answer within 3 s) is not delivery: the picks and the bar stay, with the
// waiting caption, until the receipt says accepted; a refusal leaves them with the reason and the same words can go again
async function follow(slug, st, h, sent) {
  render();
  const got = await awaitDecision(slug, h.cid);
  h.waiting = false; st.busy = false;
  // not decided in 10 minutes: the core delivers it when it frees up, a repeat would double — it is out of the owner's hands
  if (got.receipt === "accepted" || got.receipt === "stuck")
    return done(slug, sent, got.receipt === "stuck" ? "web.boards.run_stuck" : "web.boards.run_sent");
  if (got.receipt === "refused") handover.delete(slug);   // lost keeps the id: if the first one still arrives, there is one message
  const why = got.receipt === "refused" ? t("web.boards.run_failed", { slug, error: dot(String(got.error || t("web.api.rejected"))) })
    : t("web.boards.run_lost", { slug });
  st.error = why;
  if (bar[slug] === st) st.step = "run";   // the confirm box with the reason, even if it was closed while waiting
  if (bar[slug] !== st) banner(why);   // the box with the reason is not there to show it
  render();
}

async function copyList(slug) {
  const n = pickedIds(slug).length;
  try { await copyText(listText(slug)); banner(t("web.boards.copied", { count: n })); } catch { banner(t("web.boards.copy_failed")); }
}

// the bar at the bottom while the mode is on (a phone: fixed, the field is hidden under it): with picks — the actions, with none — a slim hint
function selectBar(slug) {
  const n = pickedIds(slug).length;
  if (!selecting.has(slug)) return null;
  const st = (bar[slug] = bar[slug] || {});
  const btn = (cls, text, fn, title) => { const b = el("button", "btn " + cls, text); b.type = "button"; if (title) b.title = title; b.addEventListener("click", fn); return b; };
  const foot = el("div", "select-foot" + (n ? "" : " slim"));
  foot.setAttribute("role", "region");
  foot.setAttribute("aria-label", t("web.boards.bar"));
  const cancel = btn("quiet sel-cancel-all", t("web.boards.pick_cancel"), () => leave(slug));
  if (!n) return foot.append(el("span", "hint", t("web.boards.pick_hint")), cancel), foot;
  const top = el("div", "sel-top");
  top.append(el("b", null, t("web.boards.selected", { count: n })), el("span", "sel-ids", pickedIds(slug).join(" · ")),
    btn("quiet sel-drop", t("web.boards.reset"), () => { clearPicks(slug); bar[slug] = {}; render(); }), cancel);
  const row = el("div", "sel-row");
  row.append(btn("send-ask sel-main", t("web.boards.ask", { count: n }), () => { selecting.delete(slug); bar[slug] = {}; render(); document.getElementById("msg")?.focus(); }),
    btn("sel-act" + (st.step === "run" ? " on" : ""), t("web.boards.run"), () => { st.step = st.step === "run" ? null : "run"; st.error = ""; render(); }, t("web.boards.run_title")),
    btn("sel-act sel-copy", t("web.boards.copy"), () => copyList(slug)));
  foot.append(top, row);
  if (handover.get(slug)?.waiting) { const w = el("p", "hint sel-wait", t("web.boards.run_queued")); w.setAttribute("role", "status"); foot.append(w); }
  if (st.step === "run") {
    const p = places(slug, n), text = runText(slug);
    const box = el("div", "sel-confirm");
    box.append(el("p", "sel-msg", text), el("p", "hint", p.off || p.text), ...(p.off ? [] : [el("p", "hint", t("web.boards.run_cost"))]));
    if (st.error) { const e = el("p", "sel-error", st.error); e.setAttribute("role", "alert"); box.append(e); }
    const acts = el("div", "sel-confirm-row");
    const go = btn("send-ask sel-send", t("web.boards.run_send", { slug }), () => sendRun(slug));
    go.disabled = !!p.off || !!st.busy || !!handover.get(slug)?.waiting;
    acts.append(go, btn("quiet sel-edit", t("web.boards.run_edit"), () => intoField(slug, text)),
      btn("quiet sel-cancel", t("web.boards.run_cancel"), () => { st.step = null; st.error = ""; render(); }));
    box.append(acts);
    foot.append(box);
  }
  return foot;
}

// the direction's executors not linked to a card (over the last day) — a column on the right; none — no column
function unlinkedCol(facts) {
  const list = facts?.unlinked || [];
  if (!list.length) return null;
  const col = el("section", "board-col board-unlinked");
  const h = el("h3", null, t("web.boards.unlinked") + " ");
  h.append(el("span", "n", t("web.boards.unlinked_day", { count: list.length })));
  col.append(h, el("p", "hint", t("web.boards.unlinked_hint")));
  for (const u of list.slice(0, 12)) {
    const item = el("div", "board-card static");
    const when = u.state === "working" ? t("web.boards.unlinked_working")
      : u.state === "handed" ? t("web.boards.unlinked_handed", { when: stamp(u.since) })
        : t("web.boards.unlinked_stopped", { when: stamp(u.since) });
    item.append(el("span", "meta", `${u.name} · ${when}`), el("span", "t", u.about || u.name));
    col.append(item);
  }
  if (list.length > 12) col.append(el("p", "hint", t("web.boards.unlinked_more", { count: list.length - 12 })));
  return col;
}

// the board lags behind the work (core/board.py lag): after the last card edit the lead got the owner's word or raised
// executors without a card — a line above the columns; a fact, not a verdict
function lagNote(facts) {
  const g = facts?.lag;
  if (!g) return null;
  const what = [g.delegations ? t("web.boards.lag_delegations", { count: g.delegations }) : "",
    g.owner ? t("web.boards.lag_owner", { count: g.owner }) : ""].filter(Boolean).join(", ");
  const m = Math.max(1, Math.floor(g.age / 60));
  const age = g.age < 3600 ? t("web.boards.lag_min", { m })
    : g.age < 86400 ? t("web.boards.lag_hours", { h: Math.floor(m / 60), m: m % 60 })
      : t("web.boards.lag_days", { count: Math.floor(g.age / 86400) });
  const p = el("p", "board-lag", t("web.boards.lag", { what, age }));
  p.title = t("web.boards.lag_hint");
  return p;
}

// the board did not load: what the hub said, what to do, and "Retry" — it asks again now, not at the next refresh
function errorBlock(slug, why) {
  const retry = el("button", "btn small boards-retry", t("web.boards.retry"));
  retry.type = "button";
  retry.addEventListener("click", () => {
    const c = cache[slug];
    if (c) { c.data = null; c.at = 0; }
    render();
  });
  return [el("p", "hint boards-error", why), el("p", "hint boards-error-more", t("web.boards.error_more")), retry];
}

export function boardFrame(slug, old) {
  load(slug);
  const body = old || el("div", "feed boards-body");
  body.dataset.slug = slug;
  const top = old ? old.scrollTop : 0;
  const shown = body.dataset.shown;   // the error on screen (below); any other content clears it
  body.dataset.shown = "";
  const c = cache[slug] || {}, data = c.data;
  if (!data) { body.replaceChildren(el("p", "hint", t("web.boards.loading"))); return body; }
  if (data.error) {
    // the same error is not drawn again on every redraw: "Retry" is a node a click has to survive (down and up on the same one)
    if (shown !== slug + " " + data.error) body.replaceChildren(...errorBlock(slug, data.error));
    body.dataset.shown = slug + " " + data.error;
    return body;
  }
  const board = el("div", "board");
  const known = new Set(data.statuses);
  const statuses = data.statuses.concat([...new Set(data.tasks.map((t) => t.status))].filter((x) => !known.has(x)));
  for (const st of statuses) {
    const list = data.tasks.filter((t) => t.status === st);
    const col = el("section", "board-col");
    const h = el("h3", null, st + " ");
    h.append(el("span", "n", String(list.length)));
    if (selecting.has(slug) && list.length) {
      const whole = list.every((x) => picked(slug, x.id));
      const all = el("button", "col-all", t(whole ? "web.boards.col_none" : "web.boards.col_all"));
      all.type = "button";
      all.title = t(whole ? "web.boards.col_none_title" : "web.boards.col_all_title", { column: st });
      all.addEventListener("click", () => {
        setPicks(slug, whole ? pickedList(slug).filter((p) => !list.some((x) => x.id === p.id)) : [...pickedList(slug), ...list.map(mark)]);
        anchor = null;
        render();
      });
      h.append(all);
    }
    col.append(h);
    for (const task of list) {
      col.append(card(slug, task, data.facts));
      if (c.open === task.id) {
        const d = el("div", "board-detail md");
        d.append(md(task.body || t("web.boards.no_description")));
        col.append(d);
      }
    }
    if (!list.length) col.append(el("p", "hint", t("web.boards.empty")));
    board.append(col);
  }
  const loose = unlinkedCol(data.facts);
  if (loose) board.append(loose);
  const foot = selectBar(slug);
  board.classList.toggle("picking", selecting.has(slug));
  body.replaceChildren(...[lagNote(data.facts)].filter(Boolean), board, ...(foot ? [foot] : []));
  body.scrollTop = top;
  return body;
}
