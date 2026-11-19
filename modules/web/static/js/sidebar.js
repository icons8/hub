// The session list on the left: working ones on top, sleeping and failed ones in a collapsed group; search with ⌘K.
import { $, el, icon, elapsed, ago, avatarNode, dayTime, placeNodes, chevron } from "./dom.js";
import { S, isRunner, resting, crewOf, crewNote, hubLabel, sessionLabel, sessionName } from "./state.js";
import { askRemove, confirmDlg, go } from "./dialogs.js";
import { account, post } from "./api.js";
import { bare, isolate, locale, t, CLOSED } from "./i18n.js";
import { scheduleNav } from "./schedule.js";
import { modelTitle } from "./modelpicker.js";
import { hubsDue, hubsGroup } from "./hubs.js";
import { extNavs, ordered, slotNodes, subgroups } from "./ext.js";
import { claim, mergeInto, stand } from "./reconcile.js";
import { isOpen, setOpen, foldDefault } from "./fold.js";
import { layout, explicitGroup, hasGroup, addGroup, resetLayout, renaming, renameField, keepingEditor, startRenaming,
  groupMenu, openPalette, bindDrag, isDragging, markDirty, NONE } from "./groups.js";

const GLYPH = { idle: "●", busy: "⠋", starting: "⠋", dialog: "?", held: "⌨", asleep: "·", failed: "✕", closed: "·", absent: "·" };
// the row icon follows the core status (a terminal window without a form — working, a sleeping session with a question —
// waiting); icon names and data-s keep their old values (busy, dialog…): themes color by them. Manual mode has its own
// ⌨ icon, its status is idle
const LOOK = { working: "busy", waiting: "dialog", idle: "idle", asleep: "asleep", failed: "failed" };

export const needsOwner = (s) => (s.requests || []).filter((r) => r.state === "needs-owner");
// "waiting for you" and "working" come from the core's session status (core/views.session_status: one rule for the
// page, plugins and clients): a question as a form, a card, a tool request (a badge must not show without a question
// behind it). The watchdog's guess from the result text (attention) is a soft "reply?", set only once the result has been
// read and you have done nothing; while it is unread — a dot
const waitsOwner = (s) => s.status === "waiting";

// roles: an agent is a permanent specialist, a direction is a long session on one topic, the rest are workers
const isAgent = (s) => s.role === "agent";
// a direction is an executor with a backlog (the backlog is the executor's tool); older ones come from the directions list
const isDirection = (s) => !isAgent(s) && (!!s.backlog || (S.snap.directions || []).includes(s.name));
// where to show it: an executor without a backlog goes under whoever started it (not under the hub)
// an executor's executor goes under the nearest ancestor that is itself in the list (otherwise an executor started by
// an auto-executor vanished — the list nests one level only); if the chain reaches the hub or breaks, it goes under
// the topmost executor of the chain (otherwise executors of an executor started by the hub were separate rows)
const parentOf = (s) => {
  let up = isRunner(s) ? s.raised_by : null, top = null;
  for (let i = 0; up && i < 5; i++) {
    const p = S.snap.sessions.find((x) => x.name === up);
    if (!p || p.kind === "hub") return top;
    if (!isRunner(p)) return up;
    top = up;
    up = p.raised_by;
  }
  return up ? null : top;   // a cycle or a chain longer than five — to the top level, as before
};
const hubName = () => (S.snap.sessions.find((x) => x.kind === "hub") || {}).name;
const avatar = (s) => (s.kind === "hub" ? s.icon || "🧠" : isAgent(s) ? s.icon || "🤖" : isDirection(s) ? s.icon || "🧭" : s.icon || "");   // icon may also be the session's own (own_icon)
// the group by session kind — where it lands until it is dragged elsewhere (new sessions land here too)
const natural = (s) => (s.kind === "hub" ? NONE.id : isAgent(s) ? "agents" : isDirection(s) ? "directions" : "workers");
// where a session lands under layout L: a manually assigned group wins wherever the session is; otherwise by kind
// (agents are always visible, sleeping ones too: they are on duty; only workers and directions go to "rest" when they
// sleep or fail)
const home = (s, L) => {
  const g = explicitGroup(s.name, L);
  if (g) return g;
  if (s.kind === "hub" && !resting(s)) return NONE.id;   // the hub is an ordinary row: outside groups at the top, can be put in a group
  if (!isAgent(s) && resting(s)) return "rest";
  return hasGroup(natural(s), L) ? natural(s) : NONE.id;
};
// plugins can change the order inside a group (hub.addOrder, 1.10): for example, pinned directions go on top;
// the manual order (drag and drop) is applied over that
const arrange = (id, rows, L) => {
  rows = ordered(id, rows);
  const o = L.order[id] || [];
  const pos = (r) => { const i = o.indexOf(r.name); return i < 0 ? 1e6 : i; };
  return rows.map((r, i) => [r, i]).sort((x, y) => pos(x[0]) - pos(y[0]) || x[1] - y[1]).map((x) => x[0]);
};
// who is in a group under layout L, in display order — all sessions, not only the visible rows (collapsed, search)
const membersOf = (id, L) => arrange(id, S.snap.sessions.filter((s) => !parentOf(s) && home(s, L) === id), L).map((s) => s.name);

// the group counter is just a number: red is a single sign for "waiting for you", and a session count is not that
const countBadge = (n) => el("span", "gcount", String(n));

// search by name: the matching names (Enter opens the first); their parents are shown alongside for context but did
// not match themselves. While searching, collapsed groups and executors are expanded (a click on a heading or the badge folds
// them until the query changes), and the saved collapse state is untouched
let lastQuery = "";
let searchMatches = null;   // while searching: the found ones — name → how well it matched (0 exact, 1 from the start, 2 inside)

// "Now" (like the Active toggle in Claude Code): only what is running or waiting for you — by core facts — plus the
// recently opened ones (below); everything else is in "All" (no "N more" and no group buttons). The view is stored in
// the browser (hub.sidebar), "All" by default; search covers everything
let onlyActive = localStorage.getItem("hub.sidebar") === "active";
// a result for you: an answer to your words or task (core owner_reply after seen: the latest one counts, even when a
// result it sent on its own came later) — until you open the session; one the session sent on its own (a task handed in,
// a schedule, another session's news) — for a day, then it is only a dot in "All". A plugin's "new" (hub.addUnread)
// decides both: unread is false — neither
const DAY = 864e5;
// "Mark all read": up to which result each session was marked here, until the core's snapshot says so too (a patch that
// came before the seen was applied would bring the row back for a moment)
const readTill = new Map();
const marked = (s) => {
  const v = readTill.get(s.name);
  if (v === undefined) return false;
  if ((s.seen || 0) >= v) { readTill.delete(s.name); return false; }   // the core has it now
  return v >= (s.owner_out || 0);
};
const answered = (s) => !!s.unread && !marked(s) && (s.owner_reply || 0) > (s.seen || 0);
// a plugin's "new" on a session the core holds read (core_unread false) is not bound to the day: the plugin decides
const ownNews = (s) => !!s.unread && !marked(s) && !answered(s)
  && (s.core_unread === false || Date.now() - (s.out_head_ts || 0) * 1000 < DAY);
// needs the user: waiting for an answer (form, card, tool request, terminal window), sent something new, the watchdog's guess.
// Failed ones don't count: they are in "Sleeping or failed" and in the count at the bottom (old failed sessions
// would otherwise keep the list long)
const needsYou = (s) => s.status === "waiting" || answered(s) || ownNews(s) || !!s.terminal || !!s.attention;
// its "waiting…" word (fresh — from the core) on a live or sleeping session; a failed or retired one waits for nothing
const waits = (s) => !!s.waiting && s.status !== "failed" && !s.retired;
const queued = (s) => queuedFor(s.name).length > 0 || (S.snap.queue || []).some((q) => q.wake && q.name === s.name);
// computed once per redraw (buildList): the view and who is "hot" — not per row
let now = { smart: false, hot: new Set() };
const hot = (s) => now.hot.has(s.name);
const smart = () => now.smart;
// the open session and the recently opened ones: a read session doesn't vanish as soon as you move to the next one
// (active sessions must not disappear on click) — it is kept for KEEP after leaving it; the tab memory also survives a
// reload (sessionStorage): without it a reload lost the sessions just viewed, and "Now · 18" became "16"
const KEEP = 15 * 60e3;
// broken or unavailable storage is just an empty memory, the list doesn't fail
const stored = (k, v) => { try { return JSON.parse(sessionStorage.getItem(k)) ?? v; } catch { return v; } };
const store = (k, v) => { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* none */ } };
const left = new Map(Object.entries(stored("hub.left", {})).filter(([, t]) => typeof t === "number"));   // name -> when it was left
// only what changed is written: the list is redrawn on every event
let saved = {};
function remember() {
  for (const [k, v] of [["hub.left", Object.fromEntries(left)], ["hub.wasNew", Object.fromEntries(wasNew)]]) {
    const j = JSON.stringify(v);
    if (saved[k] !== j) { saved[k] = j; store(k, v); }
  }
}
let lastSel = null, keepTimer = 0;
const near = (s) => s.name === S.selected || Date.now() - (left.get(s.name) ?? -Infinity) < KEEP;
// leaving is also recorded on an address change (this module is evaluated before main.js, so its handler runs first —
// S.selected is still the old one): the redraw may be deferred (drag and drop), and a quick A → B → C must not lose B
addEventListener("hashchange", () => { if (S.selected) { left.set(S.selected, Date.now()); remember(); } });
function noteLeaving() {
  if (lastSel !== S.selected) { if (lastSel) left.set(lastSel, Date.now()); lastSel = S.selected; }
  // once the time is up, remove the row even without new events: redraw at the nearest deadline
  clearTimeout(keepTimer);
  let due = Infinity;
  // a timestamp from the future (the clock was set back) would keep the row past its time — drop it too
  for (const [n, t] of left) { const d = t + KEEP - Date.now(); if (d <= 0 || d > KEEP) left.delete(n); else due = Math.min(due, d); }
  // "was in New" is also read by the row caption in "All" — clean it in any view: it left the view (read elsewhere, time is up)
  for (const n of [...wasNew.keys()]) if (!near({ name: n })) wasNew.delete(n);
  // a result sent on its own leaves "Now" after a day: redraw then even on a quiet hub
  for (const s of S.snap?.sessions || []) {
    const d = ownNews(s) ? (s.out_head_ts || 0) * 1000 + DAY - Date.now() : 0;
    if (d > 0) due = Math.min(due, d);
  }
  due = Math.min(due, hubsDue());   // and a hub's word sent on its own
  if (due < Infinity) keepTimer = setTimeout(() => { if (S.snap) renderList(); }, due + 50);
}
// whose executors are active or open — one pass over the list per redraw
const litParents = () => new Set(S.snap.sessions.filter((k) => hot(k) || near(k)).map(parentOf).filter(Boolean));
// executors in "Now" only when busy: working, waiting for you or for their own, sent something new, waiting for a
// queue slot, plus the open one and the recently opened; sleeping and idle ones with nothing new aren't there at all —
// not as a row, not in the counter, not behind the arrow (sleeping sessions must not expand; the same for idle ones
// with nothing to do); in "All" — everyone
const awake = (f) => hot(f) || near(f) || f.state === "held" || crewOf(f).length > 0;   // manual mode, own executors
const active = (s, lit) => s.kind === "hub" || near(s) || hot(s) || lit.has(s.name);

function group(g, rows, forks = [], total = rows.length) {
  const title = g.title, box = el("section", "sgroup");
  box.dataset.g = g.id;
  // a group collapses as a whole; when collapsed it shows a dot if anyone in it (executors included) waits for you or
  // sent something new; the state is remembered between visits
  const all = rows.concat(forks.filter((f) => rows.some((r) => r.name === parentOf(f))));
  const bare = g.id === NONE.id;   // outside groups — rows on top, with no heading
  const shut = !bare && (searchMatches ? searchShut.has("g:" + g.id) : !isOpen(g.id));   // folded ones — by id (fold.js), shared with plugins (1.31)
  const hw = el("div", "sgroup-hw");
  const h = el("button", "sgroup-h");
  h.type = "button";
  h.setAttribute("aria-expanded", String(!shut));
  const gt = el("span", "gt", title);
  if (renaming === g.id) gt.replaceChildren(renameField(g));   // rename in place — the same field (groups.js)
  else gt.addEventListener("dblclick", (e) => { if (g.id !== NONE.id) { e.preventDefault(); startRenaming(g.id); } });
  h.append(chevron(!shut), gt, countBadge(total));   // the number counts everyone in the group, in "Now" too
  if (shut) foldDot(h, all);
  h.addEventListener("click", () => { foldClick("g:" + g.id, g.id, shut); renderList(); });
  hw.append(h);
  if (g.id !== NONE.id) {
    const more = el("button", "gmore");
    more.append(el("span", "gdots"));   // three dots drawn by CSS: the "⋯" glyph sits off the button's center in most fonts
    more.type = "button";
    more.title = t("web.groups.menu_title");
    more.setAttribute("aria-label", t("web.groups.menu_aria", { title }));
    more.addEventListener("click", (e) => { e.stopPropagation(); groupMenu(more.getBoundingClientRect(), g); });
    hw.append(more);
  }
  if (bare) box.classList.add("bare"); else box.append(hw);
  if (shut) return box;
  const ul = el("ul");
  if (!rows.length && !bare) ul.append(el("li", "g-empty", t("web.groups.empty")));
  // plugin subgroups (hub.addSubgroups, 1.31) — headers in the same list: rows are dragged and dropped as without them
  const subs = bare ? null : subgroups(g.id, rows);
  if (!subs) rowsInto(ul, rows, forks, "s");   // top level of groups: such rows can be dragged and dropped onto
  else for (const sg of subs) {
    const id = `${g.id}/${sg.id}`;
    foldDefault(id, sg.open);
    const open = searchMatches ? !searchShut.has("g:" + id) : isOpen(id);
    const li = el("li", "ssub"), sh = el("button", "sgroup-h ssub-h");
    li.dataset.sub = sg.id;
    sh.type = "button";
    sh.setAttribute("aria-expanded", String(open));
    sh.append(chevron(open), el("span", "gt", sg.title), countBadge(sg.rows.length));
    if (!open) foldDot(sh, sg.rows.concat(forks.filter((f) => sg.rows.some((r) => r.name === parentOf(f)))));
    sh.addEventListener("click", () => { foldClick("g:" + id, id, !open); renderList(); });
    li.append(sh);
    ul.append(li);
    if (open) rowsInto(ul, sg.rows, forks, "s");
  }
  box.append(ul);
  return box;
}

// a folded header gets a dot if anyone under it waits for you or sent something new
function foldDot(h, all) {
  const wants = all.some((x) => waitsOwner(x) || (x.attention && !x.unread));
  if (!wants && !all.some((x) => x.unread)) return;
  const d = el("span", wants ? "fdot wants" : "fdot");
  d.title = wants ? t("web.sidebar.group_wants") : t("web.sidebar.group_fresh");
  h.append(d);
}

// top-level rows with their executors below; mark is "s" (drag and drop, groups) or "m" (row menu only)
function rowsInto(ul, rows, forks, mark) {
  for (const s of rows) {
    const all = forks.filter((f) => parentOf(f) === s.name);
    // in "Now" the rows under a parent are all the busy ones, exactly those in the counter (selected by awake, once in
    // buildList): expanding only those that need you while keeping the working ones in the counter gave a counter of
    // "3" with one visible row, and the number of rows changed with every "read / opened / waiting"
    const inQueue = queuedFor(s.name);   // queued requests of its executors — rows under it
    const li = sessionRow(s, all, null, inQueue);
    li.dataset[mark] = s.name;
    ul.append(li);
    if ((all.length || inQueue.length) && forksOpen(s, all)) {
      const sub = el("li", "forks");
      const inner = el("ul");
      // working ones above sleeping ones, queued ones last; the name is shown without the parent's name in front;
      // in "Now" those that need you come first: eight working ones don't push "waiting for you" down;
      // while searching, a better name match goes first among the rows of this parent (like top-level rows, buildList)
      // within one rank the freshest is on top (since — when it took its current state, for a sleeping one when it fell
      // asleep); without it the order was the creation order and the newest sleeping one sank to the bottom
      const rank = (f) => 3 * (searchMatches?.get(f.name) ?? 3) + (smart() && needsYou(f) ? 0 : 1 + resting(f));   // the match is the senior part (the state is 0..2)
      const fresh = (a, b) => (b.since || 0) - (a.since || 0);
      // nested rows get a key of their own (reconcile.js): data-s / data-m enable drag and the menu of a top-level row
      const own = (li, key, name) => { li.dataset[key] = name; return li; };
      inner.append(...[...all].sort((a, b) => rank(a) - rank(b) || fresh(a, b)).map((f) => own(sessionRow(f, [], s.name), "f", f.name)),
        ...inQueue.map((q) => own(queueRow(q, s), "q", q.name)));
      sub.append(inner);
      ul.append(sub);
    }
  }
}

// "Now" by state: sections by what is needed from you, top to bottom; a session goes into the first one that fits, a
// parent goes into the section of its most urgent executor. Core facts only, no model: "Waiting for you" is the core
// status, the terminal window and the watchdog's "reply?" guess (its second line is the action itself); "Answered you"
// and "Sent on their own" split the new results by the core's owner_reply; "Waiting" is its "waiting…" word plus the
// executor queue — the core doesn't know the addressee, so we don't guess. "Sent on their own" is folded at first: on a
// busy day it is 10–25 sessions (spec d2433840)
const SECTIONS = [["wait", t("web.sidebar.pill_waiting")], ["new", t("web.sidebar.sec_answered")], ["work", t("web.sidebar.sec_work")],
  ["near", t("web.sidebar.sec_near")], ["held", t("web.sidebar.sec_held")], ["own", t("web.sidebar.sec_own")]];
foldDefault("a:own", false);
// urgency for a parent is not the display order: "Just viewed" is shown above "Waiting" (owner decision), but an
// executor with a "waiting…" word lifts its parent out of "Just viewed"; one that answered you lifts it above "Working"
const RANK = { wait: 0, new: 1, work: 2, held: 3, own: 4, near: 5 };
// a session that was in "Answered you" or "Sent on their own" stays there while it is fresh (KEEP after leaving): a
// read one doesn't jump into "Recently viewed" under the cursor (active sessions must not disappear on click); the tab
// memory, like "recently viewed", survives a reload. name -> its section; an older tab kept names alone ("new")
const wasNew = new Map(Object.entries((() => { const v = stored("hub.wasNew", {}); return Array.isArray(v) ? Object.fromEntries(v.map((n) => [n, "new"])) : v || {}; })())
  .filter(([n, k]) => typeof n === "string" && (k === "new" || k === "own")));
function sectionOf(s) {
  let k = waitsOwner(s) || s.terminal || s.attention ? "wait" : answered(s) ? "new" : s.status === "working" ? "work"
    : waits(s) || queued(s) ? "held" : ownNews(s) ? "own" : near(s) ? "near" : null;
  if (k === "near" && wasNew.has(s.name)) return wasNew.get(s.name);
  // sticks only for a result that arrived while you were not in the session (opened from the section); a result seen live doesn't stick
  if ((k === "new" || k === "own") && s.unread && s.name !== S.selected) wasNew.set(s.name, k);
  else if (k !== "new" && k !== "own") wasNew.delete(s.name);
  return k;
}
// order within a section: the oldest question first, new — freshest on top, working — longest-running on top, waiting — freshest word
const asked = (s) => Math.min(...(s.pending || []).map((q) => q.since || Infinity), Infinity);
const ORDER = { wait: asked, new: (s) => -(s.owner_reply || 0), own: (s) => -(s.owner_out || 0), work: (s) => s.since || 0,
  held: (s) => -((s.waiting || {}).ts || 0), near: (s) => (s.name === S.selected ? -Infinity : -(left.get(s.name) || 0)) };

function sections(top, forks) {
  // the hub is always the first row on top, in any state (also among the active ones): a section doesn't repeat it,
  // and its executors don't lift it (one waiting for you is expanded under it); we don't keep it in "New" — a read
  // result doesn't hang as a second row. Sessions without a section go with it
  const pinned = (s) => s.kind === "hub", hubs = new Set(top.filter(pinned).map((s) => s.name));
  const at = new Map(top.map((s) => [s.name, pinned(s) ? null : sectionOf(s)]));
  for (const f of forks) {
    const p = parentOf(f), k = sectionOf(f);
    if (k && at.has(p) && !hubs.has(p) && (!at.get(p) || RANK[k] < RANK[at.get(p)])) at.set(p, k);
  }
  const nodes = [];
  const idle = top.filter((s) => !at.get(s.name)).sort((a, b) => pinned(b) - pinned(a));
  if (idle.length) {
    const box = el("section", "sgroup bare"), ul = el("ul");
    rowsInto(ul, idle, forks, "m");
    box.append(ul);
    nodes.push(box);
  }
  for (const [k, title] of SECTIONS) {
    const rows = top.filter((s) => at.get(s.name) === k).map((s) => [s, ORDER[k](s)])
      .sort((x, y) => (x[1] === y[1] ? 0 : x[1] < y[1] ? -1 : 1)).map((x) => x[0]);
    if (rows.length) nodes.push(section(k, title, rows, forks));
  }
  return nodes;
}

// a section is like a group (collapsible, remembers it), but without a menu or drag and drop: a row's place is decided by its state
function section(k, title, rows, forks) {
  const id = "a:" + k, box = el("section", "sgroup asec");
  box.dataset.sec = k;   // not data-g: rows can't be dropped there (groups.js)
  const shut = !isOpen(id);
  const h = el("button", "sgroup-h" + (k === "wait" ? " alarm" : ""));
  h.type = "button";
  h.setAttribute("aria-expanded", String(!shut));
  h.append(chevron(!shut), el("span", "gt", title), countBadge(rows.length));
  h.addEventListener("click", () => { setOpen(id, shut); renderList(); });
  if (k === "own") {   // "Mark all read": the sessions of the section and their executors with something new — at once
    const hw = el("div", "sgroup-hw"), all = el("button", "read-all", t("web.sidebar.read_all"));
    all.type = "button";
    all.title = t("web.sidebar.read_all_title");
    all.addEventListener("click", () => readAll(rows, forks));
    hw.append(h, all);
    box.append(hw);
  } else box.append(h);
  if (shut) return box;
  const ul = el("ul");
  rowsInto(ul, rows, forks, "m");
  box.append(ul);
  return box;
}

// "Mark all read" in "Sent on their own": the core's "read" for each session there and for its executors with something
// new (the same point as opening it, /api/seen up to its last result); the rows go at once, not after the next snapshot,
// and none is kept as just read (wasNew)
function readAll(rows, forks) {
  const names = new Set(rows.map((r) => r.name));
  for (const x of [...rows, ...forks.filter((f) => names.has(parentOf(f)) && f.unread)]) {
    if (x.last_out) post("/api/seen", { session: x.name, seq: x.last_out });
    readTill.set(x.name, Math.max(x.last_out || 0, x.owner_out || 0));
    wasNew.delete(x.name);
  }
  remember();
  renderList();
}

// the second line in "Now" is what is needed from you: the question itself; what is new — the start of the result
// (core: out_head) without markup. null — the row caption as in "All" (what it is working on, "waiting: …", the
// terminal window, the watchdog's guess)
// (a link cut by the core in the middle of the address is shown as text too; heading and quote only at the start, "3 > 2" is left alone)
const plain = (t) => t.replace(/\[([^\]]*)\]\([^)]*(\)|…$)/g, (_, x, end) => (end === "…" ? x + "…" : x)).replace(/\*\*(\S(?:.*?\S)?)\*\*/g, "$1")
  .replace(/`/g, "").replace(/^\s*(#{1,6}|>)\s+/, "").replace(/\s+/g, " ").trim();
// the gist is a line the session writes about itself (state the essence itself, within 160 characters): the first line
// of its long reply to you, or `hub gist`. The core serves it only while it concerns the latest reply to you; for a
// question it is the question itself; for a running turn — the gist stated in it (ts). With no fresh one, a fallback:
// the first sentence of the text, or, if it is longer, cut at a word
const GIST = 160;
const gistAfter = (s, ts) => (s.gist && ts != null && s.gist.ts >= ts ? s.gist.text : null);
// trimmed — a beginning cut by the core (out_head at the limit length, "…" at the end): then the first sentence, even if the text is shorter than the limit
const headCut = (s) => s.out_head.length >= GIST && s.out_head.endsWith("…");
function brief(t, trimmed = false) {
  const first = t.match(/^.{20,}?[.!?…](?=\s|$)/)?.[0];
  if (t.length <= GIST) return trimmed && first && first !== t ? first : t;
  if (first && first.length <= GIST) return first;
  const cut = t.slice(0, GIST - 1), sp = cut.lastIndexOf(" ");
  return (sp > GIST / 2 ? cut.slice(0, sp) : cut).replace(/[\s,;:—–…-]+$/, "") + "…";
}
function leadLine(s) {
  const q = (s.pending || [])[0];
  if (q) return q.tool === "AskUserQuestion" ? brief(plain((((q.input || {}).questions || [])[0] || {}).question || t("web.sidebar.lead_question")))
    : t("web.sidebar.lead_permission", { tool: q.tool });
  const r = needsOwner(s)[0];
  if (r) return t("web.sidebar.lead_connect", { tool: r.tool });
  if (s.terminal || s.status === "working") return null;   // the terminal window: "answer it there" stays the caption
  if (s.attention && (s.attention.ask || s.attention.text)) return brief(plain(s.attention.ask || `…${s.attention.text.slice(-80)}`));   // "reply?": the action itself
  // an answer to you that a later result sent on its own displaced: the answer's own start (reply_head; an older core
  // has none — the last result, as before)
  if ((wasNew.get(s.name) === "new" || answered(s)) && s.reply_head && s.owner_reply !== s.owner_out) return brief(plain(s.reply_head));
  return s.out_head && (s.unread || wasNew.has(s.name)) ? s.gist?.text || brief(plain(s.out_head), headCut(s)) : null;
}

// executors (nested; the names and classes .forks/.fork-toggle date from when this held agent forks): the toggle is on
// the right of the row of whoever started them, with no separate "N" row; collapsed, it still shows that one is
// waiting for action
const openForks = {};
// while searching they are open for the match, but the badge still works: a fold lasts until the query changes (the saved state stays)
// (the same for the heading of a group or a subgroup: its key is "g:" + id)
const searchShut = new Set();
// a click on a group heading: open — to open; while searching it changes only the fold of this query, otherwise the saved one
const foldClick = (key, id, open) => { if (searchMatches) searchShut[open ? "delete" : "add"](key); else setOpen(id, open); };
const forksOpen = (s, mine) => searchMatches ? !searchShut.has(s.name) : openForks[s.name] ?? (smart() ? mine.length > 0 || queuedFor(s.name).length > 0 : mine.some((f) => f.name === S.selected));
function forkToggle(s, mine, queued = []) {
  const open = forksOpen(s, mine);
  const wants = mine.some((f) => waitsOwner(f) || f.attention);       // waits for your action
  const fresh = mine.some((f) => f.unread);                           // something new arrived
  const working = mine.filter((f) => f.status === "working").length;
  const tog = el("button", "fork-toggle" + (wants && !open ? " wants" : ""));
  tog.type = "button";
  tog.setAttribute("aria-expanded", String(open));
  tog.title = [t("web.sidebar.fork_count", { count: mine.length }), working ? t("web.sidebar.fork_working", { count: working }) : "",
    queued.length ? t("web.sidebar.fork_queued", { count: queued.length }) : "", wants ? t("web.sidebar.fork_wants") : ""].filter(Boolean).join(", ")
    + ". " + (open ? t("web.sidebar.collapse") : t("web.sidebar.expand"));
  tog.append(el("span", "n", String(mine.length || queued.length)), chevron(open));
  if (!open && (wants || fresh)) tog.append(el("span", wants ? "fdot wants" : "fdot", ""));
  tog.addEventListener("click", (e) => {
    e.stopPropagation();
    if (searchMatches) searchShut[open ? "add" : "delete"](s.name); else openForks[s.name] = !open;
    renderList();
  });
  return tog;
}

// the executor queue of a mini hub (core: queue in the snapshot), three kinds of rows: a new request is a pale row under
// the mini hub; a wake of one that already delivered is its own row, "queued · rework" with a task to rework (about),
// "queued · wake-up" without one
const queuedFor = (name) => (S.snap.queue || []).filter((q) => q.by === name && !q.wake);
// a wake with a task to rework (about) is a "rework", without one a "wake-up" (as `hub status`: minihub.wake_kind)
const queueLine = (q) => t(q.wake ? (q.about ? "web.sidebar.queue_line_rework" : "web.sidebar.queue_line_wake") : "web.sidebar.queue_line", { pos: q.pos, why: q.why });
function queueRow(q, up) {
  const li = el("li");
  const b = el("div", "srow queued");
  const g = el("span", "glyph", "·");
  g.dataset.s = "queued";
  g.setAttribute("aria-hidden", "true");
  const txt = el("span", "txt"), nm = el("span", "nm", shortName(q.name, up.name));
  const av = avatar(up);   // an executor takes the icon of whoever started it — a queued one too
  if (av) nm.prepend(avatarNode(av), document.createTextNode(" "));
  txt.append(nm, el("span", "lb", queueLine(q)));
  b.title = t("web.sidebar.queue_tip", { name: q.name, task: q.task, line: queueLine(q) });
  b.append(g, txt);
  li.append(b);
  return li;
}

// a name under a parent is shown without the parent's name in front (no need to duplicate the naming): under maps-dev, maps-dev-api → api
export function shortName(name, under) {
  if (typeof under !== "string" || !under) return name;   // .map(sessionRow) used to pass an index and an array — the page crashed
  for (const p of [under, under.replace(/-/g, "")]) if (name.startsWith(p + "-")) return name.slice(p.length + 1);
  return name;
}

// a session's background split for its row: its own helper agents as one caption ("3 helpers · <model>", the models the
// host named, in the order they started; none named — the count alone), and the commands
export function bgParts(background) {
  const all = [...(background || [])].sort((a, b) => a.since - b.since);
  const agents = all.filter((x) => x.kind === "agent"), cmds = all.filter((x) => x.kind !== "agent");
  // the first two models; more — "+N" (the tooltip names each helper's own)
  const named = [...new Set(agents.map((x) => x.model).filter((m) => typeof m === "string" && m))];
  const models = named.slice(0, 2).join(", ") + (named.length > 2 ? ` +${named.length - 2}` : "");
  const helpers = !agents.length ? "" : models ? t("web.sidebar.bg_helpers_model", { count: agents.length, model: models })
    : t("web.sidebar.bg_helpers", { count: agents.length });
  return { helpers, agents, cmds };
}

function sessionRow(s, forks = [], under = null, queued = []) {
  const li = el("li", forks.length || queued.length ? "has-forks" : null);
  const b = el("button", "srow");
  b.type = "button";
  b.setAttribute("aria-current", String(!!S.cur && s.name === S.cur.name));
  // working (a turn, a launch, or background agents and commands of an idle session) — spins; you can write to it
  // right away — the message doesn't wait for the end of the turn
  const bgWork = s.state === "idle" && (s.background || []).length;   // caption: what runs in the background
  // manual mode and not yet started — by state: "idle" (a green dot) would be untrue for a session that isn't running
  // an idle session whose executors are working (a mini hub delegated and ended its turn) gets the same icon as a working one
  const crew = crewOf(s).length;
  const look = s.state === "held" ? "held" : s.state === "absent" ? "absent" : crew ? "busy" : LOOK[s.status] || "idle";
  const g = el("span", "glyph" + (look === "busy" ? " spin" : ""), GLYPH[look]);
  g.dataset.s = look;
  g.setAttribute("aria-hidden", "true");
  const txt = el("span", "txt");
  // the session name as is (not the task text); the task goes into the tooltip, and for a sleeping executor — whose
  // task it was
  const task = isRunner(s) && s.about;
  const nm = el("span", "nm", s.kind === "hub" || s.system ? sessionLabel(s) : shortName(s.name, under));
  nm.title = (s.role === "agent" || task) && s.about ? `${s.name} — ${s.about}` : s.name;
  const av = avatar(s);
  if (av) {
    const a = avatarNode(av);
    a.title = t("web.groups.change_icon");
    a.addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); openPalette(a.getBoundingClientRect(), s.name); });
    nm.prepend(a, document.createTextNode(" "));
  }
  // what exactly runs in the background and for how long (otherwise it is unclear what the session is doing): its own
  // helper agents as a count and their model ("3 helpers · <model>" — they are not rows of the list), then commands
  const { helpers, agents, cmds } = bgParts(s.background);
  const first = cmds[0] || null;
  const cmdLabel = !first ? "" : cmds.length === 1 ? t("web.sidebar.bg_one", { title: first.title || first.id, elapsed: elapsed(first.since) })
    : t("web.sidebar.bg_many", { count: cmds.length, title: first.title || first.id });
  const label = bgWork ? [helpers, cmdLabel].filter(Boolean).join(" · ") : s.label;
  // the hub's helpers take the place of "main session": the row is narrow, and the model must not be cut off
  const lb = el("span", "lb", s.kind === "hub" && !(bgWork && helpers) ? t("web.sidebar.main_session", { label })
    : s.role === "agent" && s.about ? `${label} · ${s.about}` : label);
  const base = lb.textContent;   // nothing on top (recap, result) — then "waited" is allowed
  // what it is working on now — the watchdog's recap (like the recap in Claude Code) instead of the agent description
  const recap = s.state === "busy" && s.recap && s.recap.ts >= s.since ? s.recap.text : "";
  // "Now": the gist the session stated in this turn replaces the watchdog's guess; an idle session shows its gist
  // instead of the Claude Code recap (which is longer than 160 characters)
  const said = smart() && s.state === "busy" ? gistAfter(s, s.since) : null;
  if (said) { lb.textContent = said; lb.title = `${label} · ${said}`; }
  else if (recap) { lb.textContent = recap; lb.title = `${label} · ${recap}`; }
  else if (smart() && !bgWork && s.state === "idle" && s.gist) { lb.textContent = s.gist.text; lb.title = s.gist.text; }
  // idle after a big turn — the Claude Code recap: the goal, where it stopped, what is next
  // ("Now" has no leading "idle ·" or "waiting for executors · N ·": the gist takes up to two lines, the service info
  // is carried by the icon and the counter)
  else if (s.state === "idle" && s.away && s.away.ts >= (s.since || 0) - 5) { lb.textContent = smart() && !bgWork ? s.away.text : `${label} · ${s.away.text}`; lb.title = s.away.text; }
  // its task is with executors and there is no recap — the start of its last result to you (what it delegated and to
  // whom), not "waiting for executors"; only if its last result was addressed to you (owner_out = last_out): otherwise
  // the delegation bypassed you and the start would be about something old
  else if (smart() && crew && s.state === "idle" && s.out_head && s.owner_out === s.last_out) lb.textContent = brief(plain(s.out_head), headCut(s));
  if (s.state === "busy") { const t = el("span", "elapsed", ` · ${elapsed(s.since)}`); t.dataset.since = s.since; lb.append(t); }
  // helpers alone: how long the first one runs, ticking (it is alive, not stopped); a command's caption has its own time
  else if (bgWork && agents.length && !cmds.length && lb.textContent === label) {
    const t = el("span", "elapsed", ` · ${elapsed(agents[0].since)}`); t.dataset.since = agents[0].since; lb.append(t);
  }
  if (!bgWork && helpers) lb.append(document.createTextNode(` · ${helpers}`));
  if (!bgWork && cmds.length) lb.append(document.createTextNode(` · ${t("web.sidebar.bg_count", { count: cmds.length })}`));
  if (bgWork) lb.title = s.background.map((x) => `${x.title || x.id}${x.kind === "agent" && x.model ? ` · ${x.model}` : ""} · ${elapsed(x.since)}`
    + (x.out ? `\n  ${x.out}` : "")).join("\n");
  // idle or asleep — what it waits for, in its own word (hub waiting; "idle" alone says nothing useful).
  // The rest about the direction board lives inside the session (the "Board" under the header); the list shows only a counter (more would be noisy)
  if (["idle", "asleep"].includes(s.state) && !bgWork && s.waiting) {
    lb.textContent = t("web.sidebar.waiting", { text: s.waiting.text });
    lb.title = t("web.sidebar.waiting_title", { label, ago: ago(s.waiting.ts), text: s.waiting.text });
  } else if (["idle", "asleep"].includes(s.state) && !bgWork && !crew && s.waited && lb.textContent === base) {
    // someone wrote to it after the label and it didn't repeat it — the word is stale (core, waited): a gray "waited";
    // only in place of a bare "idle/asleep" — the recap and the rest take priority; if its executors are working,
    // their "waiting for executors" takes priority
    lb.textContent = "";
    lb.append(el("span", "waiting-stale", t("web.sidebar.waited", { text: s.waited.text, at: dayTime(s.waited.stale) })));
    lb.title = t("web.sidebar.waited_title", { label, ago: ago(s.waited.ts), at: dayTime(s.waited.stale), text: s.waited.text });
  }
  // Opus's safety filter tripped again soon after the core brought it back (core/refusals.py): it stays on the fallback
  // model until the owner acts — said in the list, not only in its header; what
  // is more urgent (a question, the terminal, a stop) is drawn over it below
  if (s.refused?.state === "again") {
    lb.textContent = t("web.sidebar.refused_again");
    lb.title = t("web.main.refused_title", { asked: modelTitle(s.refused.asked), got: modelTitle(s.refused.got), at: dayTime(s.refused.ts) });
  }
  // the result was read and nothing was done — the watchdog's guess is visible; while unread — only a dot
  const guess = s.attention && !s.unread;
  // what is needed from the user replaces the state: "Say 'yes' to the merge", not "idle"
  if (guess && (s.attention.ask || s.attention.text)) {
    lb.textContent = s.attention.ask || `…${s.attention.text.slice(-80)}`;
    lb.title = s.attention.text || "";
  }
  // a Claude Code window without a turn form (terminal is a core fact): it can only be answered in the terminal — say
  // where, and show it next to "waiting for you" if there is something else answerable (a card, a tool request). hub
  // attach goes to the same window (hub take would start the process anew and the question would be lost)
  if (s.terminal) {
    lb.textContent = t("web.sidebar.terminal_wait", { name: s.name });
    lb.title = t("web.sidebar.terminal_wait_title");
  }
  if (task) {
    const who = s.raised_by && s.raised_by !== hubName() ? s.raised_by : t("web.sidebar.of_hub");
    // under whoever started it we don't write "task from them" — it is obvious
    if (s.state === "asleep" && s.retired) { lb.textContent = under ? t("web.sidebar.asleep") : t("web.sidebar.asleep_task", { who }); lb.title = `${lb.textContent}: ${s.about}`; }
  }
  // stopped at once (the owner's "Stop executor", hub session stop): not "asleep" after handing in — as in its header
  if (isRunner(s) && s.stopped && s.state === "asleep") {
    lb.textContent = s.stopped.by === "owner" ? t("web.main.stopped_you", { at: dayTime(s.stopped.ts) })
      : t("web.main.stopped_by", { name: sessionName(s.stopped.by), at: dayTime(s.stopped.ts) });
    lb.title = s.about ? `${lb.textContent}: ${s.about}` : lb.textContent;
  }
  // spinning but the executors aren't named (not a mini hub) — a note, otherwise "idle" would sit next to a spinning
  // icon; applied after the other captions so they don't erase it
  // ("Now": the note goes only onto a bare caption, the gist isn't crowded — executors show in the icon and counter)
  const note = smart() && lb.textContent !== base ? "" : crewNote(s, crew);
  if (note) lb.append(document.createTextNode(note));
  const rework = (S.snap.queue || []).find((q) => q.wake && q.name === s.name);   // the rework waits for a slot
  if (rework) { lb.textContent = queueLine(rework); b.classList.add("queued"); }
  const lead = smart() && leadLine(s);
  // the tooltip is whole: for "reply?" the watchdog's full text, not the short action
  if (lead) { lb.textContent = lead; lb.title = (s.attention && !s.terminal && s.attention.text) || lead; }
  b.classList.toggle("two", smart());   // "Now": the caption takes up to two lines (sidebar.css)
  // "Now": how long it waits for you, when the answer or the result came, how long the word "waiting…" stands — right
  // of the name (a working one has its running time in the caption, live)
  // (Clean's detailed list has a time of its own on the right; the node would only widen the caption's grid span)
  const since = !smart() || s.kind === "hub" || s.state === "busy" || document.documentElement.classList.contains("th-clean") ? null
    : s.status === "waiting" ? (asked(s) < Infinity ? asked(s) : s.since)   // the question's own time, not the turn's
    : s.attention ? s.attention.since : answered(s) ? s.reply_ts || s.out_head_ts : s.unread ? s.out_head_ts : waits(s) ? s.waiting.ts : null;
  const when = since && since < Infinity ? el("span", "now-ago ago", ago(since)) : null;   // .ago: main.js renews it
  if (when) when.dataset.ts = since;
  // its gist, question or first sentence of a reply (up to 160 characters): two lines in "Now", the whole in the tooltip;
  // the class stays for themes
  b.classList.toggle("gist", smart() && (!!lead || !!s.gist && lb.firstChild?.textContent === s.gist.text));
  // the tooltip covers the whole row and is complete: the full name and description, not a clipped "…"
  // why it spins goes into the tooltip too, if another caption overrode the note (rework, a question in "Now") or there is none (mini hub)
  const shown = lb.title || lb.textContent;
  b.title = [nm.title, shown, crew && !(note && shown.includes(note)) ? t("web.sidebar.crew_working", { crew }) : ""].filter(Boolean).join("\n");
  nm.removeAttribute("title"); lb.removeAttribute("title");
  txt.append(nm, lb);
  b.append(g, txt);
  if (when) b.append(when);   // its own column on the name's line, before the tag and the dot
  const dirInfo = isDirection(s) ? (S.snap.direction_tasks || {})[s.name] || {} : {};
  const open = dirInfo.open || 0;
  // open board tasks are written out in the caption; the "N ▸" number on the right counts executors only, for all roles
  // (task counts of directions would otherwise conflict with executor counts of specialists)
  if (open && !smart()) {   // in "Now" the caption belongs to the question, the result, the "waiting" word; the task count is in "All"
    lb.prepend(document.createTextNode(t("web.sidebar.tasks_prefix", { count: open })));   // "3 tasks", not "board 3"
    // delivered but not reviewed by the lead (core/board.py) — with the same counter as in the board header
    if (dirInfo.unsorted) lb.firstChild.textContent += t("web.sidebar.unsorted_prefix", { count: dirInfo.unsorted });
    lb.title = t("web.sidebar.board_title", { count: open }) + (lb.title ? ` · ${lb.title}` : "");
  }
  // a direction with its own icon gets 🧭 at the start of the second line: in custom groups it sits next to specialists
  // (a badge in the icon's corner looked like a bug)
  if (s.kind !== "hub" && isDirection(s) && s.icon && s.icon !== "🧭") lb.prepend(avatarNode("🧭", "dir-badge"));
  // new: a turn result arrived after you last looked at the session
  const fresh = s.unread && !(S.cur && S.cur.name === s.name);
  b.classList.toggle("unread", !!fresh);
  b.classList.toggle("blocked", !!waitsOwner(s));            // stopped without you: a colored edge and a red tag
  b.classList.toggle("rest", resting(s));
  if (waitsOwner(s)) { const w = el("span", "tag", t("web.sidebar.tag_waits")); w.title = t("web.sidebar.tag_waits_title"); b.append(w); }
  else if (guess) {
    // a click on the tag dismisses it and doesn't open the session
    const tag = el("span", "tag soft dismiss", t("web.sidebar.tag_guess"));
    tag.setAttribute("role", "button");
    tag.tabIndex = 0;
    tag.title = t("web.sidebar.tag_guess_title");
    // the row stays the same node while its markup is unchanged (reconcile.js): take the session from the snapshot at click time
    const off = (e) => {
      e.stopPropagation(); e.preventDefault();
      const cur = S.snap.sessions.find((x) => x.name === s.name);
      if (cur) cur.attention = null;
      renderList(); post("/api/dismiss", { session: s.name });
    };
    tag.addEventListener("click", off);
    tag.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") off(e); });
    b.append(tag);
  }
  else if (fresh) { const d = el("span", "dot"); d.title = t("web.sidebar.dot_title"); b.append(d); }
  b.append(...slotNodes("sidebar.session", { session: s }).map(stand));   // plugin badges on the session row: a node already in the page stays there (reconcile.js)
  // everything right of the name is for the caption; a parent of executors also has a twin column of the counter (::after)
  if (smart()) b.style.setProperty("--lb-span", `span ${b.children.length - 1 + (li.classList.contains("has-forks") ? 1 : 0)}`);
  if (searchMatches?.has(s.name)) b.dataset.searchMatch = "true";   // Enter opens the first of them from the top of the list
  b.addEventListener("click", () => go(s.name));
  li.append(b);
  if (forks.length || queued.length) {
    const t = forkToggle(s, forks, queued);
    // room for the counter is reserved by its invisible twin in the row itself (::after): the same width with any theme
    // font and text size (a fixed 44 px for everything made "waiting for you" and "15 ▸ ●" overlap)
    b.dataset.fw = [...t.querySelectorAll(".n, .chev")].map((x) => x.textContent).join(" ");
    b.classList.toggle("fw-dot", !!t.querySelector(".fdot"));
    li.append(t);
  }
  return li;
}

// the pill in the search row, before ☰: a red "Waiting for you · N" — shown when someone is stopped without you, hidden
// otherwise. A click goes to the first such session. There is no green "To review" pill (unread only) any more: unread
// is visible as a dot on the row and in "Now"
function attentionPill() {
  const p = $("wpill");
  if (!p) return;
  const list = S.snap.sessions.filter(waitsOwner), label = t("web.sidebar.pill_waiting");
  p.hidden = !list.length;
  if (!list.length) return;
  p.className = "wpill alarm";
  p.replaceChildren(el("span", "wl", label), el("span", "ws", "·"), el("b", "wn", String(list.length)));   // a narrow header hides the label, the number stays
  p.setAttribute("aria-label", `${label}: ${list.length}`);
  p.onclick = () => go(list[0].name);
  p.title = `${label}: ${list.map((s) => s.name).join(", ")}`;
}

// open questions of all sessions: cards and permission requests (pending) plus tool requests that need you.
// The pill counts sessions, the tab counts what is waiting for an answer
function openQuestions() {
  return S.snap.sessions.filter(waitsOwner)
    .reduce((n, s) => n + (s.pending || []).length + needsOwner(s).length, 0);
}

// the tab itself says that questions wait for you (the sessions of the pill, counted by their questions): "(N) <hub name>"
// in the title and a red dot on the icon; the number goes to the app badge where the browser has one. It is a state, not
// a "seen" mark: it stays on the active tab until the question is answered. The icon <link> is replaced (Chrome rereads a
// new element, not a changed href) only when the count changes — a snapshot comes several times a minute. The hub name
// comes from settings; the tab title matches the hub row
let tabWaits = null;   // null: the first snapshot syncs the app badge even at 0 (a reload keeps the OS badge)
function tabSignal() {
  const n = openQuestions(), name = hubLabel();
  const title = n ? `(${n}) ${name}` : name;
  if (document.title !== title) document.title = title;
  if (n === tabWaits) return;
  const was = tabWaits;
  tabWaits = n;
  if (!!n !== !!was) {
    document.querySelectorAll('link[rel="icon"]').forEach((l) => l.remove());   // new <link>s: Chrome rereads them
    for (const px of [16, 32, 48]) {
      const link = el("link");
      link.rel = "icon";
      link.type = "image/png";
      link.sizes = `${px}x${px}`;
      link.href = `/static/favicon/tab-${px}${n ? "-alert" : ""}.png`;
      document.head.append(link);
    }
  }
  try {   // an installed app: the number is drawn by the OS; a browser without it does nothing
    const done = n ? navigator.setAppBadge?.(n) : navigator.clearAppBadge?.();
    done?.catch?.(() => {});
  } catch { /* no badge — the title and the icon remain */ }
}

// a task for the hub via the "Auto" button: the hub layout commands do everything, there is no new core code for it
const AUTO_TASK = t("web.groups.auto_task");
// what "Auto" got — a line right under the buttons for 10 s, on any width: the answer grows from where it was pressed (the
// banner was far from it on a desktop and hidden on a phone's session list). The line is always there, empty when there is
// nothing to say: its text is set on the live node, so the built markup of the buttons does not change (reconcile.js keeps
// the row and the focus on "Auto") and a screen reader hears the change of an existing status line; kept here, as the list
// is drawn anew on every snapshot
let autoNote = null;
const autoLine = () => {
  const n = document.querySelector("#list .g-tools .g-auto-note");
  if (n && n.textContent !== (autoNote?.text || "")) n.textContent = autoNote?.text || "";
};
function autoSaid(text) {
  const until = Date.now() + 10000;
  autoNote = { text, until };
  autoLine();
  setTimeout(() => { if (autoNote?.until === until) { autoNote = null; autoLine(); } }, 10000);
}

export function renderList() {
  // we don't rebuild the list in the middle of a drag (the row under the pointer would vanish); it is rebuilt on drop.
  // The group title field survives a rebuild: the same node moves into the new heading (groups.js keepingEditor)
  if (isDragging()) { markDirty(); return; }
  keepingEditor(buildList);
}

function buildList() {
  placeNodes($("slot-sidebar-header"), slotNodes("sidebar.header"));   // in the search row, on the right before ☰
  placeNodes($("slot-sidebar-top"), slotNodes("sidebar.top"));   // plugin slots (ext.js)
  account();   // "Settings" is the first item of the ☰ menu (index.html, api.js), the flame is on it and on ☰; before navMenu: the ☰ mark
  navMenu([scheduleNav(), ...extNavs(), ...slotNodes("sidebar.nav")]);
  // plugin tiles sit above the search row and earlier in the markup: Tab follows the visual order in every browser (1.30);
  // nodes stay in place — a focused button doesn't lose focus on a new snapshot
  placeNodes($("slot-sidebar-tiles"), slotNodes("sidebar.tiles"));
  const q = $("filter").value.trim().toLowerCase();
  // how well the name matched the search: 0 exact, 1 from the start of the name, 2 somewhere inside, 3 none
  const fit = (s) => Math.min(...[s.name, ...(s.system ? [sessionLabel(s)] : []), ...(s.kind === "hub" ? [hubLabel()] : [])].map((n) => {
    n = n.toLowerCase();
    return n === q ? 0 : n.startsWith(q) ? 1 : n.includes(q) ? 2 : 3;
  }));
  const found = new Map();
  for (const s of q ? S.snap.sessions : []) {
    const f = fit(s);
    if (f < 3) found.set(s.name, f);
  }
  searchMatches = q ? found : null;
  if (q !== lastQuery) searchShut.clear();   // a new query shows the executors open again
  lastQuery = q;
  const best = new Map(found);   // a row's place follows the best match: its own or that of its executor under it
  const included = new Set(found.keys());
  if (searchMatches) for (const s of S.snap.sessions) if (searchMatches.has(s.name)) {
    // an executor stands under its parent — show the parent alongside, even if its name didn't match
    const parent = parentOf(s);   // nesting in the list is one level: under whom the row stands
    if (parent) {
      included.add(parent);
      best.set(parent, Math.min(best.get(parent) ?? 3, best.get(s.name)));
    }
  }
  // searching for a thread or agent by name — inside each group the one matching from the start of the name (exact
  // ones even higher) comes first, equal ones keep their order; the order of groups and the manual layout stay as
  // they are, so the list doesn't jump from letter to letter while typing
  const rank = (rows) => q ? [...rows].sort((a, b) => (best.get(a.name) ?? 3) - (best.get(b.name) ?? 3)) : rows;
  const all = S.snap.sessions.filter((s) => !searchMatches || included.has(s.name));
  const lay = layout();
  const forks = all.filter((s) => parentOf(s));  // under whoever started them, not in the common groups
  const every = all.filter((s) => !parentOf(s));
  noteLeaving();
  now = { smart: onlyActive && !q,
    hot: new Set(S.snap.sessions.filter((s) => needsYou(s) || s.status === "working" || queued(s) || waits(s)).map((s) => s.name)) };
  const lit = litParents();
  const top = smart() ? every.filter((s) => active(s, lit)) : every;
  // the toggle numbers cover the whole fleet, not what the search found
  const fleet = q ? S.snap.sessions.filter((s) => !parentOf(s)) : every;
  activeBar(fleet.length, smart() ? top.length : fleet.filter((s) => active(s, lit)).length);
  const place = new Map(every.map((s) => [s.name, home(s, lay)]));
  const total = {};   // total in the group — for its number
  for (const s of every) total[place.get(s.name)] = (total[place.get(s.name)] || 0) + 1;
  const by = {};
  for (const s of top) (by[place.get(s.name)] ||= []).push(s);
  const rested = by.rest || [];
  const restKids = forks.filter((f) => rested.some((r) => r.name === parentOf(f)));
  const list = $("list");
  const wasOpen = list.querySelector("details")?.open || rested.concat(restKids).some((s) => s.name === S.selected) || !!q;
  // the hub goes first among rows outside groups, until it is moved
  const free = (by[NONE.id] || []).sort((a, b) => (b.kind === "hub") - (a.kind === "hub"));
  // the list is built whole, not in parts: built in parts, it briefly consisted of the hub alone, and any layout read
  // in the middle (a plugin in a row, getBoundingClientRect) cut the scroll to zero — "the list jumps to the top". It
  // is installed with a single replaceChildren only on rename; normally it is merged into the previous one (mergeInto)
  // — the guarantee there is different: merging doesn't read layout, and the build happens in detached nodes
  const nodes = smart() ? sections(top, forks.filter(awake)) : [group(NONE, rank(arrange(NONE.id, free, lay)), forks)];
  remember();   // after sections: it marks "was in New"
  for (const g of smart() ? [] : lay.groups) {
    const rows = by[g.id] || [];
    if (!rows.length && (!lay.custom || q)) continue;   // empty groups are visible once the layout has been touched
    nodes.push(group(g, rank(arrange(g.id, rows, lay)), forks, total[g.id] || 0));
  }
  if (rested.length && !smart()) {
    const det = el("details", "resting");
    det.open = wasOpen;
    // the same look as group headings (all collapsible headers are the same size)
    const sum = el("summary", "sgroup-h");
    sum.append(chevron(wasOpen), el("span", "gt", t("web.sidebar.group_resting")), countBadge(rested.length));
    det.addEventListener("toggle", () => { const c = det.querySelector(":scope > summary .chev"); c.textContent = det.open ? "▾" : CLOSED; if (det.open) c.dataset.down = "1"; else delete c.dataset.down; });   // not sum: the heading may have been replaced in place (reconcile.js)
    det.append(sum);
    // sleeping rows aren't draggable, but they have a row menu; their executors are under them, as in groups (a
    // sleeping hub executor brings its own here), and "Remove all" also takes the sleeping ones among them (first) —
    // otherwise they would surface as rows after the parent is removed
    const ul2 = el("ul");
    rowsInto(ul2, rank(rested), forks, "m");
    // the names are in the markup and read at the press: the button stays the same node across snapshots (reconcile.js)
    const all2 = el("button", "btn quiet danger rest-all");
    all2.type = "button";
    all2.dataset.names = JSON.stringify(restKids.filter(resting).concat(rested).map((s) => s.name));
    all2.append(icon("close"), document.createTextNode(t("web.sidebar.remove_all")));
    all2.addEventListener("click", () => askRemove(JSON.parse(all2.dataset.names)));
    det.append(ul2, all2);
    nodes.push(det);
  }
  // the linked hubs (the hub bus): once there is one (hubs.js); folds like a group, the choice is remembered (fold.js)
  const hubs = q ? null : hubsGroup(isOpen("a:hubs"), (open) => { setOpen("a:hubs", open); renderList(); },
    smart() ? (c) => near({ name: ":hub:" + c.name }) : null);   // "Now": the open hub and the just viewed stay
  if (hubs) nodes.push(hubs);
  if (!all.length && q) nodes.push(el("p", "hint", t("web.sidebar.nothing_found")));
  const tools = el("div", "g-tools");
  const add = el("button", "btn quiet add-group", t("web.groups.add"));
  add.type = "button";
  add.addEventListener("click", () => addGroup());
  tools.append(add);
  if (lay.custom) {
    const reset = el("button", "btn quiet", t("web.groups.reset"));
    reset.type = "button";
    reset.title = t("web.groups.reset_title");
    reset.addEventListener("click", resetLayout);
    tools.append(reset);
  }
  const hub = S.snap.sessions.find((x) => x.kind === "hub");
  if (hub) {
    const auto = el("button", "btn quiet auto", t("web.groups.auto"));
    auto.type = "button";
    auto.title = t("web.groups.auto_title");
    auto.addEventListener("click", () => confirmDlg(t("web.groups.auto_dlg_title"),
      t("web.groups.auto_dlg_text"), t("web.groups.auto_send"), async () => {
        const to = S.snap.sessions.find((x) => x.kind === "hub") || hub;   // the button lives across snapshots (reconcile.js)
        const r = await post("/api/send", { session: to.name, text: AUTO_TASK, action: true });   // like the hub form: an ordinary user message
        autoSaid(r.error || t("web.groups.auto_sent"));
      }, "primary"));
    tools.append(auto);
    const note = el("p", "hint g-auto-note");
    note.setAttribute("role", "status");
    tools.append(note);
  }
  // groups are in "All"; in "Now" a row's place is decided by its state. While searching the list is a filter, not the groups:
  // the buttons give way to a line saying so
  // (nothing found — that line alone is the whole answer)
  if (!smart() && (!q || all.length)) nodes.push(q ? el("p", "hint g-tools-note", t("web.groups.search_off")) : tools);
  const keep = list.scrollTop;
  // the group title field moves into the new heading (groups.js) — while a rename is in progress, the list is replaced
  // whole; otherwise rows whose markup didn't change stay the same nodes (reconcile.js: a click isn't lost on a redraw)
  if (renaming) { list.replaceChildren(...nodes); claim(list); } else mergeInto(list, nodes);
  list.scrollTop = keep;
  autoLine();   // a line put in anew (another look, a rename) gets its text back
  attentionPill();
  awayStrip();
  queuePausedStrip();
  tabSignal();
  placeNodes($("aside-foot"), [...slotNodes("sidebar.footer"), stateCounts()]);   // plugin nodes stay in place: a field in the footer keeps focus
  if (list.scrollTop !== keep) list.scrollTop = keep;   // once more after the footer plugins: they may reorder the rows
}

// "Settings" and the sections (Schedule, Flow, plugin views, the sidebar.nav slot) sit behind the ☰ button in the search
// row, over the list: they used to take half of the list, and the row with the hub name takes space in the header. The
// items are live nodes (captions, plugin icons), which is why this is not menu.js with its text items; the behavior is
// the same: arrows, Esc and a click outside close it, Tab moves on and closes it, focus survives a redraw (a snapshot
// rebuilds the items even while the menu is open)
const NAV = ".srow.nav, a[href]";   // a section item: choosing it closes the menu; other plugin buttons in the slot don't
const navItems = () => [...$("nav-extra").querySelectorAll("button, a[href], [tabindex]")];
function navMenu(nodes) {
  const b = $("navmenu"), box = $("nav-extra");
  const at = box.contains(document.activeElement) ? navItems().indexOf(document.activeElement) : -1;
  // "Settings" (#conn) is the permanent first item. Items are the same nodes across snapshots (ext.js, schedule.js,
  // plugin slots): they are put in place, not recreated — an item under the mouse keeps its hover and animation
  const conn = [...box.children].filter((n) => n.id === "conn");
  const want = [...conn, ...nodes];
  for (const n of [...box.children]) if (!want.includes(n)) n.remove();
  want.forEach((n, i) => { if (box.children[i] !== n) box.insertBefore(n, box.children[i] || null); });
  if (at >= 0) navItems()[Math.min(at, navItems().length - 1)]?.focus({ preventScroll: true });
  if (!b) return;
  // a section from the menu is open — the button is marked, like the row of an open session
  b.setAttribute("aria-current", String(!!box.querySelector('.srow.nav[aria-current="true"]')));
  if (b.dataset.bound) return;
  b.dataset.bound = "1";
  b.addEventListener("click", () => {
    if (!box.hidden) return shutNav(false);
    // below the search row and not lower than the panel
    const top = b.closest(".search-row").getBoundingClientRect().bottom - box.parentElement.getBoundingClientRect().top;
    box.style.top = `${top}px`;
    box.style.maxHeight = `calc(100% - ${top + 8}px)`;
    box.hidden = false;
    b.setAttribute("aria-expanded", "true");
    navItems()[0]?.focus({ preventScroll: true });
  });
  box.addEventListener("click", (e) => { if (e.target.closest(NAV)) shutNav(true); });   // a section was chosen
  box.addEventListener("keydown", (e) => {
    const all = navItems(), i = all.indexOf(document.activeElement);
    const to = { ArrowDown: i + 1, ArrowUp: i - 1, Home: 0, End: all.length - 1 }[e.key];
    if (to === undefined || !all.length) return;
    e.preventDefault();
    all[(to + all.length) % all.length].focus();
  });
  box.addEventListener("focusout", (e) => { if (e.relatedTarget && !e.relatedTarget.closest("#nav-extra, #navmenu")) shutNav(false); });
  document.addEventListener("pointerdown", (e) => { if (!e.target.closest("#nav-extra, #navmenu")) shutNav(false); }, true);
  // Esc in the menu closes it and only it: not an answer to a question and not a stop of the open session's turn
  // (main.js); with focus outside the menu, Esc goes to whoever it is meant for
  addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || box.hidden || !document.activeElement?.closest("#nav-extra, #navmenu")) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    shutNav(true);
  }, true);
  addEventListener("hashchange", () => shutNav(false));
}
// close the menu; if focus was inside it, move focus to the ☰ button rather than nowhere
function shutNav(focus) {
  const b = $("navmenu"), box = $("nav-extra");
  if (box.hidden) return;
  const inside = box.contains(document.activeElement);
  box.hidden = true;
  b?.setAttribute("aria-expanded", "false");
  if (focus || inside) b?.focus();
}

// the toggle above the list: "All · N | Now · M" — top-level rows
function activeBar(n, a) {
  let bar = $("active-bar");
  if (!bar) {   // the buttons are created once for good: a redraw doesn't knock focus off them
    bar = el("div", "active-bar");
    bar.id = "active-bar";
    bar.setAttribute("role", "group");
    bar.setAttribute("aria-label", t("web.sidebar.bar_aria"));
    for (const on of [false, true]) {
      const x = el("button");
      x.type = "button";
      x.dataset.on = String(on);
      x.addEventListener("click", () => {
        onlyActive = on;
        searchShut.clear();
        for (const k in openForks) delete openForks[k];   // what was expanded by hand in one view doesn't carry over to the other
        on ? localStorage.setItem("hub.sidebar", "active") : localStorage.removeItem("hub.sidebar");
        renderList();
      });
      bar.append(x);
    }
    document.querySelector("aside .search-row").after(bar);
  }
  const [all, act] = bar.children;
  // the base theme shows the word alone: the number is in the tooltip and in the name a screen reader reads; Clean splits
  // the number into its own badge itself. Isolate marks of a right-to-left page are dropped before the number is cut off
  const clean = document.documentElement.classList.contains("th-clean");
  for (const [b, text, title, k] of [[all, t("web.sidebar.bar_all", { n }), t("web.sidebar.bar_all_title"), n],
    [act, t("web.sidebar.bar_active", { n: a }), t("web.sidebar.bar_active_title"), a]]) {
    b.textContent = clean ? text : bare(text).replace(/\s*·\s*\d+$/, "");
    b.title = `${title} · ${isolate(k)}`;
    if (clean) b.removeAttribute("aria-label");
    else b.setAttribute("aria-label", bare(text));
  }
  all.setAttribute("aria-pressed", String(!onlyActive));
  act.setAttribute("aria-pressed", String(onlyActive));
}

// "away" mode: session forms don't become cards but pile up until the user returns — always visible (an /away without
// a deadline could be forgotten for a day, leaving the forms unseen)
function awayStrip() {
  const box = $("away"), o = S.snap.owner || {};
  box.hidden = o.state !== "away";
  if (box.hidden) return;
  // short, on one line with the button (a five-line strip crowded the list); details go into the tooltip
  const n = o.deferred || 0;
  const text = t("web.sidebar.away_since", { at: dayTime(o.away_since) }) + (o.until ? t("web.sidebar.away_until", { at: dayTime(o.until) }) : "")
    + (n ? t("web.sidebar.away_questions", { count: n }) : "");
  if (box.dataset.text === text) return;
  box.dataset.text = text;
  box.title = t("web.sidebar.away_title", { since: new Date(o.away_since * 1000).toLocaleString(locale) });
  const b = el("button", null, t("web.sidebar.away_back"));
  b.type = "button";
  b.addEventListener("click", () => { b.disabled = true; post("/api/owner/back", {}); });
  box.replaceChildren(el("span", "t", text), b);
}

// executor queue pause (core: queue_paused in the snapshot — who, why, how many are waiting): a strip above the list,
// where the queue rows stand; how to lift it is stated in the strip itself, not in a tooltip
function queuePausedStrip() {
  const box = $("queue-paused"), p = S.snap.queue_paused;
  box.hidden = !p;
  if (!p) return;
  const text = t("web.sidebar.queue_paused", { by: p.by || t("web.sidebar.queue_paused_owner"), why: p.why || t("web.sidebar.queue_paused_no_why") })
    + (p.queued ? t("web.sidebar.queue_paused_waiting", { count: p.queued }) : "");
  const key = text + "|" + p.ts;
  if (box.dataset.text === key) return;
  box.dataset.text = key;
  box.title = t("web.sidebar.queue_paused_title", { since: dayTime(p.ts) });
  const lines = el("div", "t");
  lines.append(el("span", null, text), el("span", null, t("web.sidebar.queue_paused_resume")));
  box.replaceChildren(lines);
}

// footer: states as words and numbers, with the same sign as in the list (color is not the only signal); zero counts are not shown
function stateCounts() {
  const sess = S.snap.sessions;
  // every session is counted exactly once, the sum is the number of sessions; an unknown or empty status counts as idle
  const by = (st) => sess.filter((s) => s.status === st).length;
  const wait = by("waiting"), work = by("working"), fail = by("failed"), rest = by("asleep");
  const free = sess.length - wait - work - fail - rest;
  const box = el("div", "counts");
  box.setAttribute("aria-label", t("web.sidebar.counts_aria"));
  for (const [s, n, text] of [["wait", wait, t("web.sidebar.count_wait", { count: wait })], ["work", work, t("web.sidebar.count_work", { count: work })],
      ["free", free, t("web.sidebar.count_free", { count: free })], ["fail", fail, t("web.sidebar.count_fail", { count: fail })],
      ["rest", rest, t("web.sidebar.count_rest", { count: rest })]]) {
    if (!n) continue;
    const item = el("span", null, text);
    item.dataset.s = s;
    box.append(item);
  }
  if (!box.childElementCount) box.append(el("span", null, t("web.sidebar.no_sessions")));
  return box;
}

const FILTER_WAIT = 200;   // ms of typing pause in the panel search before the list is rebuilt
export function bindSearch() {
  bindDrag($("list"), natural, (name) => S.snap.sessions.find((x) => x.name === name), membersOf);
  const f = $("filter");
  // rebuild the list after a typing pause, not on every letter: rebuilding a big fleet takes up to a second and input lagged
  // (measured 26.10: INP up to 1.15 s). Enter and Esc act immediately on what is typed
  let wait = 0;
  const now = () => { clearTimeout(wait); wait = 0; if (S.snap) renderList(); };
  f.addEventListener("input", () => { clearTimeout(wait); wait = setTimeout(now, FILTER_WAIT); });
  f.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      if (wait) now();
      $("list").querySelector(searchMatches ? '.srow[data-search-match="true"]' : ".srow")?.click();
    }
    if (e.key === "Escape") { f.value = ""; now(); $("msg")?.focus(); }
  });
}

addEventListener("hub-redraw", () => { if (S.snap) renderList(); });   // a plugin changed its own state (hub.redraw, 1.10)
