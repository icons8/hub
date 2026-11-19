// Free-form session groups and a custom icon for each (docs/spec-session-groups.md). The layout lives in the core
// (layout.set): one for all browsers and devices; here are the edits applied over the snapshot until the snapshot shows
// them, plus the menu, the palette and drag-and-drop.
import { el, avatarNode, kit } from "./dom.js";
import { S, render, sessionLabel } from "./state.js";
import { banner, post } from "./api.js";
import { locale, t } from "./i18n.js";
import { hubMenu } from "./hubs.js";
import { lifeItems, stopItems } from "./dialogs.js";

// the default groups — the same keys as hub.addOrder (core/layout.py DEFAULT_GROUPS)
export const DEFAULT_GROUPS = [{ id: "agents", title: t("web.sidebar.group_agents") }, { id: "directions", title: t("web.sidebar.group_directions") },
  { id: "workers", title: t("web.sidebar.group_workers") }];
export const NONE = { id: "none", title: t("web.groups.none") };
// collapsed groups used to be remembered by the names of the default ones (localStorage hub.closedGroups) — name → id
// (the old names are the titles in the owner's language, as the browser remembers them)
export const OLD_KEYS = { [t("web.sidebar.group_agents")]: "agents", [t("web.sidebar.group_directions")]: "directions", [t("web.sidebar.group_workers")]: "workers" };

const DEFAULT = () => ({ custom: false, groups: DEFAULT_GROUPS, place: {}, order: {} });
const fromSnap = () => {
  const v = S.snap?.layout;
  return v && Array.isArray(v.groups) ? { custom: true, groups: v.groups, place: v.place || {}, order: v.order || {} }
    : DEFAULT();
};
const snapRev = () => S.snap?.layout?.rev || 0;
const copy = (L) => ({ custom: L.custom, groups: L.groups.map((g) => ({ ...g })), place: { ...L.place },
  order: Object.fromEntries(Object.entries(L.order).map(([g, n]) => [g, [...n]])) });
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// An edit is a function over the layout: applied to the latest snapshot at the moment of sending. A sent edit stays
// pending until the snapshot shows a revision greater than its base (it was written — ours or someone else's, it does
// not matter); until then subsequent edits wait here and are shown on top of it, and afterwards are recomputed from the
// fresh snapshot with its revision. This way an edit from another tab is not overwritten: base always comes from the
// snapshot, and the layout from the same one. If it never arrives (15 s) — the view comes from the snapshot, without a
// word about an error: the page cannot see a core refusal on the queue, and the core may have written it later.
export const timing = { wait: 15000, confirmed: 60000 };   // confirmed — the write is confirmed (seq), we wait for the stream
let flying = null, held = [];   // flying — {base, L, until, seq}: sent, the snapshot has not shown it yet
const apply = (L, ops) => ops.reduce((acc, op) => op(copy(acc)), L);
// the view is recomputed only on change: the snapshot's layout (object), the sent edit, the number of waiting ones
let memo = null;
export function layout() {
  settle();
  const src = S.snap?.layout || null, ses = S.snap?.sessions;   // waiting edits read the session list too
  if (memo && memo.src === src && memo.ses === ses && memo.flying === flying && memo.held === held.length) return memo.out;
  const L = flying ? flying.L : fromSnap();
  const out = held.length ? apply(L, held) : L;
  memo = { src, ses, flying, held: held.length, out };
  return out;
}
// the core confirmed the write (a reply with seq) — it is written once the snapshot reaches this seq; not confirmed
// (202 queued) — once the layout revision in the snapshot is greater than base
const written = (f) => (f.seq ? (S.snap?.seq || 0) >= f.seq : snapRev() > f.base);
function settle() {
  if (!flying?.until) return;   // no reply to the send yet
  if (written(flying)) { flying = null; queueMicrotask(flush); }
  else if (Date.now() > flying.until) {   // with a seq the write is confirmed — we wait longer (the stream lags), but not forever
    const confirmed = flying.seq;
    flying = null;
    held = [];
    memo = null;
    banner(confirmed ? t("web.groups.late_confirmed") : t("web.groups.late_unconfirmed"));
    queueMicrotask(render);
  }
}
async function flush() {
  if (flying || !held.length) return;
  const base = snapRev(), was = fromSnap(), L = apply(was, held);
  held = [];
  memo = null;
  if (same(L, was)) { render(); return; }   // the edit changes nothing (its group was already deleted, etc.)
  const f = flying = { base, L, until: 0 };
  render();
  const r = await post("/api/layout", { layout: L.custom ? { groups: L.groups, place: L.place, order: L.order } : null, base });
  if (flying !== f) return;
  if (r.error) {   // the core refused with a message, or no connection: our own copy yields to the snapshot
    flying = null;
    held = [];
    memo = null;
    banner(r.error);
    render();
    return;
  }
  f.seq = r.data?.seq || 0;   // a core "written" reply carries seq; 202 queued does not
  const wait = f.seq ? timing.confirmed : timing.wait;
  f.until = Date.now() + wait;
  setTimeout(() => { if (flying === f) { settle(); render(); } }, wait + 50);
  render();
}
function commit(op) {   // op(L) → the new layout (L is our own copy, it may be mutated)
  held.push(op);
  if (flying) render(); else flush();
}
const edit = (fn) => commit((L) => { fn(L); return { ...L, custom: true }; });
export const iconOf = (name) => (S.snap?.sessions || []).find((x) => x.name === name)?.own_icon || "";
export const hasGroup = (id, L = layout()) => L.groups.some((g) => g.id === id);
export const explicitGroup = (name, L = layout()) =>
  (L.place[name] === NONE.id || hasGroup(L.place[name], L) ? L.place[name] : null);   // NONE — outside groups, at the top

// ---- layout changes ----
// who is in a group right now (per layout L), in display order; supplied by sidebar.js (bindDrag): that is where the rules for which session goes where live
let membersOf = () => [];
// a session's natural group is determined by its look; the assignment to it is not stored (a new session takes its
// place by itself). The group's order comes from those in it now (a collapsed group and search do not interfere —
// all sessions are taken, not the rows on screen), plus the insertion, plus the previous names that are in the
// snapshot in any state; deleted ones do not accumulate
export function moveSession(name, gid, before, natural) {
  edit((L) => {
    if (gid === natural) delete L.place[name]; else L.place[name] = gid;   // NONE — sits at the top outside groups
    const names = membersOf(gid, L).filter((n) => n !== name);
    const at = before ? names.indexOf(before) : -1;
    names.splice(at < 0 ? names.length : at, 0, name);
    // sleeping, failed and closed group members (they are not among the displayed ones) keep their place: they stay
    // after their neighbor in the previous order; only deleted ones — which are not in the snapshot — go away
    // previous names are kept only for those put to rest (live members are already in names; live ones from other groups do not belong here)
    const known = new Set((S.snap?.sessions || []).filter((s) => ["asleep", "failed"].includes(s.status))   // as in home()
      .map((s) => s.name));
    const saved = (L.order[gid] || []).filter((n) => n !== name && known.has(n));
    for (const n of saved) {
      if (names.includes(n)) continue;
      let at2 = 0;
      for (let j = saved.indexOf(n) - 1; j >= 0; j--) {
        const k = names.indexOf(saved[j]);
        if (k >= 0) { at2 = k + 1; break; }
      }
      names.splice(at2, 0, n);
    }
    for (const g of Object.keys(L.order)) {   // its place in the previous group is no longer needed
      L.order[g] = L.order[g].filter((n) => n !== name);
      if (!L.order[g].length) delete L.order[g];
    }
    L.order[gid] = names;
  });
}
const newId = () => "g-" + Math.random().toString(36).slice(2, 8);
export function addGroup(name) {   // name — put that session into it right away ("Move to…" → "New group…")
  const id = newId();
  renaming = id;
  editor = null;
  edit((L) => {
    L.groups.push({ id, title: t("web.groups.new_group") });
    if (name) { L.place[name] = id; L.order[id] = [name]; }
  });
  return id;
}
export function renameGroup(id, title) {
  renaming = null;
  editor = null;
  const t = title.trim().slice(0, 60), g = layout().groups.find((x) => x.id === id);
  if (g && t && t !== g.title) edit((L) => { const x = L.groups.find((q) => q.id === id); if (x) x.title = t; });
  else render();
}
export function deleteGroup(id) {
  edit((L) => {
    L.groups = L.groups.filter((g) => g.id !== id);
    for (const n of Object.keys(L.place)) if (L.place[n] === id) L.place[n] = NONE.id;   // sessions go to the top, outside groups
    delete L.order[id];
  });
}
export function moveGroup(id, before) {
  edit((L) => {
    const g = L.groups.find((x) => x.id === id);
    if (!g) return;
    L.groups = L.groups.filter((x) => x !== g);
    const at = before ? L.groups.findIndex((x) => x.id === before) : -1;
    L.groups.splice(at < 0 ? L.groups.length : at, 0, g);
  });
}
export async function setIcon(name, emoji) {
  const r = await post("/api/session/icon", { session: name, icon: emoji || "" });
  if (r.error) banner(r.error);
}
export const resetLayout = () => commit(() => DEFAULT());

// ---- renaming in place ----
// The field is a single node for the whole rename: the list is redrawn as usual (snapshots keep coming), while the
// field moves into the new header of the same group with the typed text and focus; if the group is gone, the field is removed.
export let renaming = null;
let editor = null, moving = false;   // editor — {id, inp}; moving — the list is being rebuilt, losing focus is not the end
export const stopRenaming = () => { renaming = null; editor = null; render(); };
export function startRenaming(id) { renaming = id; editor = null; render(); }
export function renameField(g) {
  if (editor?.id === g.id) return editor.inp;
  const inp = el("input", "gt-edit");
  inp.value = g.title;
  inp.setAttribute("aria-label", t("web.groups.name_aria"));
  let done = false;
  const finish = (ok) => {
    if (done || editor?.inp !== inp) return;
    done = true;
    ok ? renameGroup(g.id, inp.value) : stopRenaming();
  };
  inp.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Enter") finish(true); if (e.key === "Escape") finish(false); });
  inp.addEventListener("blur", () => { if (!moving) finish(true); });
  inp.addEventListener("click", (e) => e.stopPropagation());
  editor = { id: g.id, inp };
  setTimeout(() => { if (editor?.inp === inp) { inp.focus(); inp.select(); } }, 0);
  return inp;
}
// build() rebuilds the list (sidebar.js renderList): the field with its focus and selection moves to the new place
export function keepingEditor(build) {
  const inp = editor?.inp, had = !!inp && document.activeElement === inp;
  const sel = had ? [inp.selectionStart, inp.selectionEnd, inp.selectionDirection] : null;
  moving = true;
  try { build(); } finally { moving = false; }
  if (!renaming) return;
  if (!editor?.inp.isConnected) { renaming = null; editor = null; return; }   // the group is gone
  if (had && document.activeElement !== editor.inp) { editor.inp.focus(); editor.inp.setSelectionRange(...sel); }
}

// ---- popup menus and the palette ----
let pop = null;
export function closePop() { pop?.remove(); pop = null; }
function popup(cls, anchor, title) {
  closePop();
  pop = el("div", "gpop " + cls);
  pop.setAttribute("role", "menu");
  if (title) pop.append(el("div", "gpop-t", title));
  document.body.append(pop);
  const place = () => {
    if (matchMedia("(max-width: 760px)").matches) return;   // on a phone it is a bottom sheet, via CSS
    const r = pop.getBoundingClientRect(), x = anchor.x ?? anchor.left, y = anchor.y ?? anchor.bottom + 4;
    pop.style.left = Math.max(8, Math.min(x, innerWidth - r.width - 8)) + "px";
    pop.style.top = Math.max(8, Math.min(y, innerHeight - r.height - 8)) + "px";
  };
  pop._place = place;
  return pop;
}
const outside = (e) => { if (pop && !e.target.closest(".gpop") && !e.target.closest(".avatar, .head-av")) closePop(); };
document.addEventListener("pointerdown", outside, true);
addEventListener("keydown", (e) => { if (e.key === "Escape" && pop) { if (!pop._esc?.()) closePop(); e.stopPropagation(); } }, true);

function items(p, list) {
  for (const it of list) {
    if (it.sep) { p.append(el("hr")); continue; }
    const b = el("button", "gpop-i" + (it.danger ? " danger" : ""));
    b.type = "button";
    b.setAttribute("role", "menuitem");
    if (it.title) b.title = it.title;
    b.append(el("span", "gi-l", it.label), ...(it.mark ? [el("span", "gi-m", it.mark)] : []));
    b.addEventListener("click", () => { it.onClick(); });
    p.append(b);
  }
  p._place();
}

// a menu at a point for another module (the hubs of the bus): the same popup and items as the rows' own
export function menuAt(anchor, title, list) {
  const p = popup("menu", anchor, title);
  items(p, list);
}

// row menu: right click or a long press
export function rowMenu(anchor, s, natural) {
  const p = popup("menu", anchor, sessionLabel(s));
  const top = () => {
    p.replaceChildren(el("div", "gpop-t", sessionLabel(s)));
    items(p, [
      { label: t("web.groups.move_to"), mark: "›", onClick: moveTo },
      { label: t("web.groups.icon_item"), mark: iconOf(s.name) || "☺", onClick: () => { closePop(); openPalette(anchor, s.name); } },
      ...acts(),
    ]);
  };
  // the session's own actions, as in its header: stop, sleep, dismiss, remove (dialogs.js); a click closes the menu first.
  // The menu may have stood open while the session changed: the click does what the session allows now (a turn that has
  // ended is not stopped), found by the item's words
  const own = (x) => [...stopItems(x), ...lifeItems(x)];
  const acts = () => {
    const list = own(s).map((it) => ({ ...it, onClick: () => {
      closePop();
      const now = S.snap?.sessions?.find((x) => x.name === s.name);
      if (now) own(now).find((x) => x.label === it.label)?.onClick();
      render();
    } }));
    return list.length ? [{ sep: true }, ...list] : [];
  };
  const moveTo = () => {
    p.replaceChildren(el("div", "gpop-t", t("web.groups.move_title", { name: sessionLabel(s) })));
    const cur = explicitGroup(s.name) || natural;
    items(p, [
      { label: t("web.groups.loose"), mark: cur === NONE.id ? "✓" : "", onClick: () => { closePop(); moveSession(s.name, NONE.id, null, natural); } },
      ...layout().groups.map((g) => ({ label: g.title, mark: g.id === cur ? "✓" : "", onClick: () => { closePop(); moveSession(s.name, g.id, null, natural); } })),
      { sep: true },
      { label: t("web.groups.new_group_item"), onClick: () => { closePop(); addGroup(s.name); } },
    ]);
  };
  top();
}
export function groupMenu(anchor, g) {
  const p = popup("menu", anchor, g.title);
  items(p, [
    { label: t("web.groups.rename"), onClick: () => { closePop(); startRenaming(g.id); } },
    { label: t("web.groups.ungroup"), onClick: () => { closePop(); deleteGroup(g.id); } },
  ]);
}

const EMOJI = ["🤖", "🧭", "🎨", "⚙️", "🛠", "💬", "📊", "🔎", "📱", "🧪", "🚀", "📦", "🔒", "💰", "🧾", "📝", "🏠", "🛒",
  "📣", "🎧", "🧠", "⭐", "🔥", "🌱", "🧹", "🗂", "✈️", "🎮", "📚", "🐞", "🧩", "☕"];
const PAGE_MARKS = ["⏰", "🔔", "⛔"];   // the page's own signs: a theme draws them too, but they are not session icons
const bare = (e) => e.replace(/\uFE0F/g, "");   // an emoji with and without U+FE0F is one icon on the screen
// the palette: the theme's pictures first — one cell per picture (a theme may draw many emoji with one), without the page's
// signs and keys the core does not take as an icon (letters, digits: core/layout.py icon_ok) — then the page's own list
function paletteIcons() {
  const out = [], seen = new Set(), urls = new Set();
  const take = (e, url) => {
    if (seen.has(bare(e)) || (url && urls.has(url))) return;
    seen.add(bare(e)); if (url) urls.add(url); out.push(e);
  };
  for (const [e, url] of Object.entries(kit.avatars))
    if (!PAGE_MARKS.includes(bare(e)) && !/[\sA-Za-z0-9]/.test(e)) take(e, url);
  for (const e of EMOJI) take(e, kit.avatars[e]);
  return out;
}
const first = (t) => { const seg = [...new Intl.Segmenter(locale, { granularity: "grapheme" }).segment(t.trim())][0]; return seg ? seg.segment : ""; };
// The search of the palette: any emoji by a word of the hub's language or English. The names and keywords are CLDR's
// (tools/emoji_names.py), static/emoji/<language>.json: {"<emoji>": "name|keyword|…"}, fetched at the first letter, once.
// A row keeps the lowercased words of both languages; the name of the emoji is shown as the tooltip.
const FOUND_MAX = 80;
let words = null;
function loadWords() {
  words ??= (async () => {
    const got = await Promise.all([...new Set([locale, "en"])].map(async (l) => {
      const r = await fetch(new URL(`../emoji/${l}.json`, import.meta.url));   // a network error is not cached: the next letter asks again
      if (r.status === 404) return null;   // a language without a file: the other one is enough
      if (!r.ok) throw new Error(`emoji names: ${r.status}`);
      return await r.json();
    }));
    if (!got.some(Boolean)) throw new Error("emoji names");
    const rows = new Map();
    for (const d of got.filter(Boolean)) for (const [e, text] of Object.entries(d)) {
      const w = text.split("|"), row = rows.get(e) || rows.set(e, { e, name: w[0], terms: [] }).get(e);
      row.terms.push(...w.map((x, i) => [x.toLowerCase(), i === 0]));
    }
    return [...rows.values()];
  })().catch((x) => { words = null; throw x; });
  return words;
}
// the rows with every word of the query in them, the best first (equal — in the order of the Unicode list): a whole name or
// keyword, then the start of one, the start of a word inside one, then anywhere in it; a name before a keyword
function search(rows, query) {
  const tokens = query.normalize("NFC").toLowerCase().split(/\s+/).filter(Boolean), out = [];
  for (const row of rows) {
    let sum = 0;
    for (const w of tokens) {
      let best = 99;
      for (const [term, isName] of row.terms) {
        const at = term.indexOf(w);
        if (at >= 0) best = Math.min(best, (term === w ? 0 : at === 0 ? 2 : term[at - 1] === " " ? 4 : 5) + (isName ? 0 : 1));
      }
      if (best === 99) { sum = -1; break; }
      sum += best;
    }
    if (sum >= 0) out.push([sum, row]);
  }
  return out.sort((a, b) => a[0] - b[0]).slice(0, FOUND_MAX).map((x) => x[1]);
}
export function openPalette(anchor, name) {
  const p = popup("palette", anchor, t("web.groups.palette_title"));
  const grid = el("div", "pal-grid");
  const cur = iconOf(name);
  const pick = (e) => { closePop(); setIcon(name, e); };
  const cell = (e, label) => {
    const b = el("button", "pal-e" + (cur && bare(e) === bare(cur) ? " on" : ""));
    b.type = "button";
    b.append(avatarNode(e, "pal-av"));
    b.setAttribute("aria-label", label || e);
    if (label) b.title = label;
    b.addEventListener("click", () => pick(e));
    return b;
  };
  const cells = paletteIcons().map((e) => cell(e));
  grid.append(...cells);
  // the search sits above the grid and replaces its cells while there is a word in it; Enter takes the first found, Esc clears
  const find = el("div", "pal-find");
  const q = el("input");
  q.placeholder = t("web.groups.emoji_search");
  q.setAttribute("aria-label", t("web.groups.emoji_search"));
  q.setAttribute("enterkeyhint", "search");
  q.autocomplete = "off";
  q.spellcheck = false;
  find.append(q);
  let turn = 0;
  const draw = async () => {
    const mine = ++turn, text = q.value.trim();
    if (!text) { grid.replaceChildren(...cells); grid.scrollTop = 0; return []; }
    let list = [], note = "";
    try { list = search(await loadWords(), text); } catch { note = t("web.groups.emoji_failed"); }
    if (mine !== turn || !p.isConnected) return [];
    grid.replaceChildren(...(list.length ? list.map((r) => cell(r.e, r.name)) : [el("div", "pal-none", note || t("web.groups.emoji_none"))]));
    grid.scrollTop = 0;
    return list;
  };
  q.addEventListener("input", draw);
  q.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter" || e.isComposing || e.keyCode === 229) return;   // Enter that ends an IME composition (Safari: after it) is not "take the first"
    e.preventDefault();
    const text = q.value.trim();
    let first1 = null;
    try { first1 = text ? search(await loadWords(), text)[0] : null; } catch { /* the field shows the note */ }
    if (first1 && p.isConnected) pick(first1.e);
  });
  p._esc = () => { if (!q.value) return false; q.value = ""; draw(); return true; };
  const own = el("div", "pal-own");
  const inp = el("input");
  inp.placeholder = t("web.groups.own_placeholder");
  inp.setAttribute("aria-label", t("web.groups.own_aria"));
  const ok = el("button", "btn", t("web.groups.done"));
  ok.type = "button";
  // an empty field just closes; if something is typed — set the first icon from it
  const apply = () => { const v = first(inp.value); closePop(); if (v) setIcon(name, v); };
  ok.addEventListener("click", apply);
  inp.addEventListener("keydown", (e) => { if (e.key === "Enter") apply(); });
  own.append(inp, ok);
  const reset = el("button", "pal-reset", cur ? t("web.groups.remove_icon") : t("web.groups.default_icon"));
  reset.type = "button";
  reset.disabled = !cur;
  reset.addEventListener("click", () => { closePop(); setIcon(name, ""); });
  p.append(find, grid, own, reset);
  p._place();
  if (!matchMedia("(pointer: coarse)").matches) q.focus({ preventScroll: true });   // a finger: the keyboard would cover the sheet
}

// ---- mouse dragging (pointer events, not HTML5 DnD: the same in all browsers; on touch — a menu on long press) ----
let drag = null;
export const isDragging = () => !!drag;
let dirty = false;
export const markDirty = () => { dirty = true; };
const MOVE = 6, HOLD = 450;
// near the top and bottom edge of the list, dragging scrolls it by itself: an EDGE px zone, the per-frame step grows from SLOW to FAST toward the edge
const EDGE = 40, SLOW = 2, FAST = 22;
let hold = null, holdAt = { x: 0, y: 0 };

function spot(x, y) {
  const list = document.getElementById("list");
  // a pointer above or below the list (but within its width) counts as at the very edge: a group can be carried to the top and bottom
  const lr = list.getBoundingClientRect();
  if (x >= lr.left && x <= lr.right) y = Math.min(Math.max(y, lr.top + 1), lr.bottom - 1);
  const under = document.elementFromPoint(x, y);
  if (!under || !list.contains(under)) return null;
  if (drag.kind === "group") {
    const boxes = [...list.querySelectorAll(".sgroup[data-g]")].filter((b) => b.dataset.g !== drag.id && b.dataset.g !== NONE.id);   // "No group" is not a place for a group: above it goes the first of the groups
    const hit = boxes.find((b) => y < b.getBoundingClientRect().top + b.getBoundingClientRect().height / 2);
    const ref = hit || boxes[boxes.length - 1];
    if (!ref) return null;
    const r = ref.getBoundingClientRect();
    return { before: hit ? hit.dataset.g : null, y: hit ? r.top : r.bottom, box: null };
  }
  const row = under.closest("li[data-s]");
  const box = under.closest(".sgroup[data-g]");
  if (!box) return null;
  const ul = box.querySelector(":scope > ul");
  if (row && row.dataset.s !== drag.name && ul?.contains(row)) {
    const r = row.getBoundingClientRect(), up = y < r.top + r.height / 2;
    if (up) return { g: box.dataset.g, before: row.dataset.s, y: r.top, box };
    let bottom = row, nxt = row.nextElementSibling;
    if (nxt?.classList.contains("forks")) { bottom = nxt; nxt = nxt.nextElementSibling; }
    if (nxt?.dataset.s === drag.name) { nxt = nxt.nextElementSibling; if (nxt?.classList.contains("forks")) nxt = nxt.nextElementSibling; }
    return { g: box.dataset.g, before: nxt?.dataset.s || null, y: bottom.getBoundingClientRect().bottom, box };
  }
  if (row && row.dataset.s === drag.name) return { g: box.dataset.g, same: true, box, y: row.getBoundingClientRect().top };
  // the header or the empty area of a group: to the start (above the first row) or, below the rows, to the end
  const first1 = ul?.querySelector(":scope > li[data-s]:not(.dragging)");
  const hr = box.querySelector(".sgroup-h")?.getBoundingClientRect();
  if (under.closest(".sgroup-h, .sgroup-hw") && first1) return { g: box.dataset.g, before: first1.dataset.s, y: first1.getBoundingClientRect().top, box };
  const last = ul ? [...ul.children].filter((c) => !c.classList.contains("dragging")).pop() : null;
  return { g: box.dataset.g, before: null, y: last ? last.getBoundingClientRect().bottom : (hr ? hr.bottom : box.getBoundingClientRect().bottom), box };
}

function showSpot(t) {
  const list = document.getElementById("list");
  list.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target"));
  let line = list.querySelector(".drop-line");
  if (!t || t.same) { line?.remove(); drag.spot = null; return; }
  if (!line) { line = el("div", "drop-line"); list.append(line); }
  const lr = list.getBoundingClientRect();
  line.style.top = (t.y - lr.top + list.scrollTop - 1) + "px";
  t.box?.classList.add("drop-target");
  drag.spot = t;
}

function begin(e, info) {
  drag = { ...info, x0: e.clientX, y0: e.clientY, on: false, down: e, pointer: e.pointerId, px: e.clientX, py: e.clientY };
}
// the drop target by the pointer's last position — after scrolling (its own or by wheel) there are other rows under it
const refresh = () => { if (drag?.on) showSpot(spot(drag.px, drag.py)); };
// autoscroll: frame by frame while dragging goes on; with the pointer held at an edge the list scrolls even without mouse movement
function autoscroll() {
  if (!drag?.on) return;
  const list = document.getElementById("list"), r = list.getBoundingClientRect();
  let dy = 0;
  if (drag.px >= r.left && drag.px <= r.right) {
    const up = r.top + EDGE - drag.py, down = drag.py - (r.bottom - EDGE);
    if (up > 0) dy = -(SLOW + (FAST - SLOW) * Math.min(1, up / EDGE));
    else if (down > 0) dy = SLOW + (FAST - SLOW) * Math.min(1, down / EDGE);
  }
  if (dy) {
    const was = list.scrollTop;
    list.scrollTop = was + (dy < 0 ? Math.floor(dy) : Math.ceil(dy));
    if (list.scrollTop !== was) refresh();
  }
  drag.raf = requestAnimationFrame(autoscroll);
}
function lift(e) {
  drag.on = true;
  const src = drag.src;
  // the pointer is captured by the row until release, even outside the window; if lost (the window went to the background, a system Esc) — cancel
  try { src.setPointerCapture(drag.pointer); } catch { /* the pointer is already released */ }
  drag.lost = () => end(false);
  src.addEventListener("lostpointercapture", drag.lost);
  const g = src.cloneNode(true);
  g.classList.add("drag-ghost");
  g.style.width = src.getBoundingClientRect().width + "px";
  document.body.append(g);
  drag.ghost = g;
  drag.li = src.closest("li, .sgroup");
  drag.li.classList.add("dragging");
  document.body.classList.add("is-dragging");
  drag.raf = requestAnimationFrame(autoscroll);
}
function end(commit) {
  if (!drag) return;
  const d = drag;
  drag = null;
  cancelAnimationFrame(d.raf);
  if (d.lost) d.src.removeEventListener("lostpointercapture", d.lost);
  try { d.src.releasePointerCapture(d.pointer); } catch { /* it was not captured or is already released */ }
  d.ghost?.remove();
  d.li?.classList.remove("dragging");
  document.body.classList.remove("is-dragging");
  const list = document.getElementById("list");
  list.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target"));
  list.querySelector(".drop-line")?.remove();
  if (commit && d.on && d.spot) {
    suppressClick = true;
    setTimeout(() => { suppressClick = false; }, 0);
    if (d.kind === "group") moveGroup(d.id, d.spot.before);
    else moveSession(d.name, d.spot.g, d.spot.before, d.natural);
  } else if (d.on) { suppressClick = true; setTimeout(() => { suppressClick = false; }, 0); }
  if (dirty) { dirty = false; render(); }
}
let suppressClick = false;

// naturalOf(s) — the session's group by its look (supplied by sidebar.js); sessionOf — a session by name. Group rows
// (li[data-s]) are draggable; the menu is available for the hub and for sleeping sessions too (li[data-m])
const ROW = "li[data-s] > .srow, li[data-m] > .srow";
const rowName = (row) => row.parentElement.dataset.s || row.parentElement.dataset.m;
const HUB = "li[data-hub] > .srow";   // an external hub of the bus: its own menu (hubs.js), not a session's
export function bindDrag(list, naturalOf, sessionOf, members) {
  membersOf = members;
  list.addEventListener("click", (e) => { if (suppressClick) { e.stopPropagation(); e.preventDefault(); } }, true);
  list.addEventListener("contextmenu", (e) => {
    const row = e.target.closest(ROW);
    const h = e.target.closest(".sgroup-hw");
    const hub = !row && e.target.closest(HUB);
    if (hub) { e.preventDefault(); hubMenu({ x: e.clientX, y: e.clientY }, hub.parentElement.dataset.hub); return; }
    if (row) { e.preventDefault(); const s = sessionOf(rowName(row)); if (s) rowMenu({ x: e.clientX, y: e.clientY }, s, naturalOf(s)); }
    else if (h && hasGroup(h.parentElement.dataset.g)) { e.preventDefault(); groupMenu({ x: e.clientX, y: e.clientY }, layout().groups.find((g) => g.id === h.parentElement.dataset.g)); }
  });
  list.addEventListener("scroll", refresh, { passive: true });   // wheel and autoscroll: recompute the drop target
  list.addEventListener("pointerdown", (e) => {
    if (drag) end(false);   // the previous drag did not finish (released where the page could not see) — abandoned
    if (e.button > 0 || e.target.closest(".avatar, .tag, .fork-toggle, .gmore, .ssub-h, input")) return;   // a plugin subgroup is not a group: it is not dragged
    const row = e.target.closest(ROW);
    const h = e.target.closest(".sgroup-h");
    const hub = !row && !h && e.target.closest(HUB);
    if (!row && !h && !hub) return;
    if (e.pointerType !== "mouse") {   // touch: a long press opens the menu, no dragging
      const x = e.clientX, y = e.clientY;
      holdAt = { x, y };
      clearTimeout(hold);
      hold = setTimeout(() => {
        hold = null;
        suppressClick = true;
        setTimeout(() => { suppressClick = false; }, 600);
        if (hub) hubMenu({ x, y }, hub.parentElement.dataset.hub);
        else if (row) { const s = sessionOf(rowName(row)); if (s) rowMenu({ x, y }, s, naturalOf(s)); }
        else { const g = layout().groups.find((q) => q.id === h.closest(".sgroup").dataset.g); if (g) groupMenu({ x, y }, g); }
      }, HOLD);
      return;
    }
    if (hub) return;   // a hub row is not dragged
    const gid = h?.closest(".sgroup")?.dataset.g;
    if (row?.parentElement.dataset.s) begin(e, { kind: "session", src: row, name: row.parentElement.dataset.s, natural: naturalOf(sessionOf(row.parentElement.dataset.s)) });
    else if (!row && hasGroup(gid)) begin(e, { kind: "group", src: h.closest(".sgroup"), id: gid });   // "No group" cannot be dragged
  });
  addEventListener("pointermove", (e) => {
    if (hold && Math.hypot(e.clientX - holdAt.x, e.clientY - holdAt.y) > 8) { clearTimeout(hold); hold = null; }   // the finger moved — it is a scroll
    if (!drag) return;
    // no button pressed — it was released where the page could not see (outside the window, in another tab): cancel, before lift-off too
    if (e.buttons === 0) { end(false); return; }
    if (!drag.on) {
      if (Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) < MOVE) return;
      // a plugin with its own dragging has already taken the press (its handler runs after ours and sets preventDefault):
      // two drags at once are not wanted — we yield, and "Move to…" in the row menu remains
      if (drag.down.defaultPrevented) { drag = null; return; }
      lift(e);
    }
    drag.px = e.clientX;
    drag.py = e.clientY;
    drag.ghost.style.left = e.clientX + 12 + "px";
    drag.ghost.style.top = e.clientY - 14 + "px";
    showSpot(spot(e.clientX, e.clientY));
  });
  addEventListener("pointerup", () => { clearTimeout(hold); hold = null; end(true); });
  addEventListener("pointercancel", () => { clearTimeout(hold); hold = null; end(false); });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && drag) end(false); });
  addEventListener("blur", () => end(false));
}
