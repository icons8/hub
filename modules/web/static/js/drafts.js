// What the owner was writing in each thread outlives a move to another thread and a page reload: the text of the
// field (S.drafts), the "Reply" target with its quote (S.replyTarget) and the id of a send whose outcome is unknown
// (unsent). Kept in localStorage per draft key; old entries go away by themselves, a sent message clears its own at
// once. Attachments are not kept (files are not stored in the browser): an "[Image #N]" mark stays in the text as the
// owner typed it.
// Two tabs of the hub do not wipe each other: a write puts only the entries this tab changed on top of a fresh read
// of the storage, and a change made in another tab is taken from the "storage" event.
import { S, render } from "./state.js";
import { boxes } from "./boxes.js";

export const DRAFT_TTL = 14 * 864e5, UNSENT_TTL = 864e5;   // the core remembers a send's client_id for a day
const MAX = 200, TEXT_MAX = 100000, QUOTE_MAX = 4000, DELAY = 400;

// entries younger than ttl, the newest max of them: {name: {at, …}}
export function prune(all, now, ttl, max = MAX) {
  const out = {};
  const live = Object.entries(all && typeof all === "object" ? all : {})
    .filter(([, v]) => v && typeof v === "object" && Number.isFinite(v.at) && now - v.at < ttl && v.at <= now + 60000)
    .sort((a, b) => b[1].at - a[1].at).slice(0, max);
  for (const [k, v] of live) out[k] = v;
  return out;
}
// the stored map with this tab's changes on top: changed[name] = entry, or null — removed here
export function merge(stored, changed) {
  const out = { ...(stored && typeof stored === "object" ? stored : {}) };
  for (const [k, v] of Object.entries(changed)) { if (v) out[k] = v; else delete out[k]; }
  return out;
}

function read(key, ttl) {
  try { return prune(JSON.parse(localStorage.getItem(key) || "{}"), Date.now(), ttl); } catch { return {}; }
}
// one stored map: changes wait DELAY (typing writes once per pause), a removal is written at once — a sent message must
// not come back after a reload and go out a second time. other(all) — another tab wrote the map: all is what is
// stored now, with this tab's unwritten changes on top (what is being typed here is not replaced by the other tab).
// A page that is going away writes nothing more: the reload aborts a send in flight, the composer takes that for a
// failure and puts the text back into the field — kept, it would come back after the reload though it most likely went out
function box(key, ttl, other = () => {}) {
  const changed = {};
  let timer = 0, leaving = false;
  const all = () => merge(read(key, ttl), changed);
  const write = (next) => {
    if (Object.keys(next).length) localStorage.setItem(key, JSON.stringify(next));
    else localStorage.removeItem(key);
  };
  const flush = () => {
    clearTimeout(timer); timer = 0;
    if (!Object.keys(changed).length) return;
    try { write(prune(all(), Date.now(), ttl)); } catch {
      // no storage or it is full — the new text is not kept this time, but a removal still goes: what is stored minus
      // the removed entries is shorter than what is there, and a sent draft must not come back after a reload
      try {
        const gone = Object.keys(changed).filter((k) => !changed[k]);
        if (gone.length) { const left = read(key, ttl); for (const k of gone) delete left[k]; write(left); }
      } catch { /* no storage at all */ }
    }
    for (const k of Object.keys(changed)) delete changed[k];
  };
  addEventListener("pagehide", () => { flush(); leaving = true; });
  addEventListener("pageshow", () => { leaving = false; });   // back from the browser's page cache
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flush(); });
  addEventListener("storage", (e) => { if (e.key === key || e.key === null) other(all()); });
  return {
    all,
    put(name, v) {
      if (leaving) return;
      changed[name] = v ? { ...v, at: Date.now() } : null;
      if (!v) flush();
      else if (!timer) timer = setTimeout(flush, DELAY);
    },
  };
}
// bring the in-memory map `mine` to what is stored now; pick(entry) — the value of an entry, or undefined for a
// broken one; ours(name) — the name is one this map stores, so its absence in storage means it was removed (a name
// that is not ours and not stored is left as it is). Returns the names that changed
function adopt(mine, all, pick, ours = () => true) {
  const names = [];
  for (const k of new Set([...Object.keys(mine), ...Object.keys(all)])) {
    if (!(k in all) && !ours(k)) continue;
    const v = all[k] ? pick(all[k]) : undefined;
    if (JSON.stringify(mine[k]) === JSON.stringify(v)) continue;
    if (v === undefined) delete mine[k]; else mine[k] = v;
    names.push(k);
  }
  return names;
}

// which draft keys are kept: the session fields (dock.js names them) and whatever is already stored. A plugin's field
// (hub.input) stores its own draft, as before
const kept = new Set();
// field drafts: S.drafts keeps its shape for every module that reads and writes it (composer, attachments, boards)
const text = (v) => (typeof v.text === "string" && v.text ? v.text : undefined);
const texts = {};
const drafts = box("hub.drafts", DRAFT_TTL, (all) => {
  // the other tab sent or changed the draft: the field here shows the same, not a text that has already gone out. Only
  // kept drafts and stored ones: a plugin's field, a board's or a linked hub's lives in S.drafts too, never stored
  for (const k of adopt(texts, all, text, (k) => kept.has(k))) { kept.add(k); const b = boxes.get(k); if (b?.ta?.isConnected) b.refresh(); }
});
for (const k of adopt(texts, drafts.all(), text)) kept.add(k);
S.drafts = new Proxy(texts, {
  set(o, k, v) {
    const cut = typeof v === "string" ? v.slice(0, TEXT_MAX) : "";
    if (kept.has(k) && (o[k] || "") !== cut) drafts.put(k, cut ? { text: cut } : null);
    o[k] = v;
    return true;
  },
  deleteProperty(o, k) { if (kept.has(k) && o[k]) drafts.put(k, null); delete o[k]; return true; },
});
// the field with this draft key is a session's: its draft is kept from now on (what it already holds too — text put
// there before the field was first drawn)
export function keep(k) {
  if (kept.has(k)) return;
  kept.add(k);
  if (typeof texts[k] === "string" && texts[k]) drafts.put(k, { text: texts[k].slice(0, TEXT_MAX) });
  if (unsent.has(k)) ids.put(k, unsent.get(k));
}

// the "Reply" target belongs to the feed it was chosen in: another thread has its own (or none), coming back brings it back
const target = (v) => (v.target && typeof v.target === "object" && typeof v.target.session === "string" ? v.target : undefined);
const targets = {};
// the feeds whose target came from storage (before a reload, from another tab), not chosen in this tab
const restored = new Set();
const replies = box("hub.reply", DRAFT_TTL, (all) => {
  const names = adopt(targets, all, target);
  for (const n of names) restored.add(n);
  if (names.length) render();
});
for (const n of adopt(targets, replies.all(), target)) restored.add(n);
const here = () => S.cur?.name;
// a target kept from before a reload whose addressee is no longer in the snapshot (closed, renamed) is dropped: the
// words would go to someone who is not there. Only in a session's own feed and once the snapshot has sessions; a target
// chosen in this tab is the owner's own choice of a moment ago and stays as before
function gone(n, v) {
  if (!restored.has(n)) return false;
  const all = S.snap?.sessions;
  if (!Array.isArray(all) || !all.length) return false;
  const names = new Set(all.map((x) => x.name));
  return names.has(v.view) && !names.has(v.session);
}
// a long quote is stored cut (the session reads its first 150 characters, the field shows 400): one quoted message
// must not take the room of every draft in the storage
const short = (v) => (typeof v.quote === "string" && v.quote.length > QUOTE_MAX ? { ...v, quote: v.quote.slice(0, QUOTE_MAX) + "…" } : v);
// the target of one feed, whichever feed is open: a voice message recorded in it and stopped on another session's page
// goes with the reply of its own feed (dock.js aim), not with the open one's
export function replyIn(n) {
  const v = n != null && targets[n];
  if (!v) return null;
  if (gone(n, v)) { delete targets[n]; restored.delete(n); replies.put(n, null); return null; }
  return v;
}
export function dropReply(n) {
  if (n == null) return;
  restored.delete(n);
  if (targets[n]) { delete targets[n]; replies.put(n, null); }
}
Object.defineProperty(S, "replyTarget", {
  get() { return replyIn(here()); },
  set(v) {
    const n = v?.view ?? here();
    if (!v) return dropReply(n);
    if (n == null) return;
    restored.delete(n);
    targets[n] = v; replies.put(n, { target: short(v) });
  },
  enumerable: true, configurable: true,
});

// a send with an unknown outcome (composer.js): draft key -> {sig, client_id}. Kept with the draft that came back into
// the field: sent again unchanged after a reload, it goes with the same id, and the core does not deliver it twice
const ids = box("hub.unsent", UNSENT_TTL);
const id = (v) => (typeof v.sig === "string" && typeof v.client_id === "string" ? { sig: v.sig, client_id: v.client_id } : undefined);
class Unsent extends Map {
  set(k, v) { if (kept.has(k)) ids.put(k, v); return super.set(k, v); }
  delete(k) { if (kept.has(k) && this.has(k)) ids.put(k, null); return super.delete(k); }
}
export const unsent = new Unsent();
for (const [k, v] of Object.entries(ids.all())) if (id(v)) { kept.add(k); Map.prototype.set.call(unsent, k, id(v)); }
