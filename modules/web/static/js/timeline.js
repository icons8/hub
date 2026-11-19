// The feed of the open session is kept apart from the snapshot (the streamed snapshot was megabytes per event, ~90% of
// it the feeds of all sessions). The snapshot carries only each session's feed revision (timeline_rev) and the reader
// (epoch); the page asks /api/timeline for the rows changed after its own revision and merges them by key k: a tool step
// row changes in place ("running" -> "done"). A different reader (page server restart, new journal) — the whole feed.
import { S, renderLive } from "./state.js";
import { failedOnce } from "./backoff.js";

const LIMIT = 400;   // feed rows kept in page memory per session; older ones load on scroll (/api/history)
const tl = Object.create(null); // session -> {key, epoch, rev, items, busy, again}
// whose feed it is: the page server run (epoch) plus the conversation itself (session_id) — a session with the same name
// but a different conversation, or a new server run, gets its feed anew; a late reply to an earlier fetch is dropped
const identity = (snap, name) => {
  const session = snap?.sessions?.find((s) => s.name === name && s.state !== "removed");
  return session && snap.epoch ? JSON.stringify([snap.epoch, name, session.session_id ?? null]) : null;
};
const current = (name, t, snap = S.snap) => !!t && t.key === identity(snap, name);
function discard(name) {
  const t = tl[name];
  delete tl[name];
  if (t) { clearTimeout(t.timer); t.cancel?.(); }
}

// the page server is older than this page (JS is served from disk at once, the server only after a restart) — it
// sends the feeds in the snapshot itself, as before; it has no /api/timeline
const oldServer = () => !!(S.snap && !S.snap.epoch);

export const loaded = (name) => oldServer() || !!(current(name, tl[name]) && tl[name].epoch === S.snap.epoch);
// the last feed fetch failed (network, non-200 reply): the feed shows the connection status (main.js feed)
export const failed = (name) => !!(current(name, tl[name]) && tl[name].error);
// when the next automatic attempt at a failed feed starts (ms); the pause grows by Fibonacci (backoff.js) and a
// snapshot does not shorten it — otherwise every hub event would be one more failed attempt and the count would run away
export const nextTry = (name) => (current(name, tl[name]) && tl[name].nextAt) || 0;
// only a feed that has not loaded yet waits out a pause: a loaded one that failed to refresh retries with the next hub
// event, as before, and a press on the status or "Retry" (t.press) does not wait — also when it came during a fetch
const paused = (t) => !t.epoch && t.error && !t.press && t.nextAt - Date.now() > 50;
// the automatic attempts are for the feed on screen (api.js loads that one); another one is fetched when it is opened
const shown = (name) => (S.cur?.name || S.selected) === name;
// a press on the status: the failure is cleared at once (the feed shows "Loading…" again) and the fetch runs anew;
// the failure is not cleared by the automatic attempts — otherwise the feed would flicker "Loading…" / status on every try
export function retry(name) {
  if (tl[name]) { tl[name].error = false; tl[name].press = true; }
  renderLive();
  sync(name);
}
// the connection is back or the tab is in front again: the feed on screen that did not load tries right now; `fresh`
// (the network or the stream is back) starts the pauses over, a mere return to the tab does not
export function retryFailed(fresh) {
  for (const [name, t] of Object.entries(tl)) {
    if (!t.error || t.busy || !current(name, t) || !shown(name)) continue;
    if (fresh) t.fails = 0;
    t.nextAt = 0;
    sync(name);
  }
}
// nothing exists before what was served (fresh session, the whole feed): false; unknown (old server) — undefined
export const olderKnown = (name) => current(name, tl[name]) ? tl[name].older : undefined;

// the streamed snapshot carries no feeds — fill in the ones the page already knows
export function adopt(snap) {
  for (const [name, t] of Object.entries(tl)) if (!current(name, t, snap)) discard(name);
  if (!snap.epoch) return;  // old server: the feeds are already in the snapshot
  snap.timelines = {};
  for (const [name, t] of Object.entries(tl)) if (t.epoch === snap.epoch) snap.timelines[name] = t.items;
}

// in an open session that is being read above the bottom, old rows are not dropped: every row dropped from the top
// shortened everything above the reading position and the text jumped. The cap is LIMIT × 4; once the reader is
// back at the bottom, the next revision trims to LIMIT
function merge(items, fresh, name) {
  const at = new Map(items.map((it, i) => [it.k, i]));
  for (const it of fresh) {
    if (at.has(it.k)) items[at.get(it.k)] = it;
    else { at.set(it.k, items.length); items.push(it); }
  }
  items.sort((a, b) => a.k - b.k);
  const keep = S.reading?.(name) ? LIMIT * 4 : LIMIT;
  if (items.length > keep) S.trimmed?.(name);
  return items.slice(-keep);
}

// bring the feed of `name` to the revision from the snapshot; one fetch per session at a time, revisions that arrive
// during it wait for the next one
export async function sync(name) {
  if (!S.snap || !name || name.startsWith(":") || oldServer()) return;
  const key = identity(S.snap, name);
  if (tl[name] && !current(name, tl[name])) discard(name);
  if (!key) return;
  const want = (S.snap.timeline_rev || {})[name] || 0;
  const t = tl[name] || (tl[name] = { key, epoch: null, rev: 0, items: [] });
  if (t.epoch === S.snap.epoch && t.rev >= want) return;
  if (t.busy) { t.again = true; return; }
  if (paused(t)) return;   // the pause timer, retryFailed and a press end it
  clearTimeout(t.timer);
  t.timer = null;
  t.busy = true;
  t.press = false;
  const epoch = S.snap.epoch, controller = new AbortController();
  const valid = () => tl[name] === t && current(name, t);
  let rejectRead;
  const cancelled = new Promise((_, reject) => { rejectRead = reject; });
  t.cancel = () => { controller.abort(); rejectRead(new Error("Timeline read cancelled")); };
  // the deadline covers the whole reply, body read included: a hung connection can't hold the fetch forever (abort may not fire)
  const timeout = setTimeout(t.cancel, 15000);
  try {
    const since = t.epoch === epoch ? t.rev : 0;
    const q = new URLSearchParams({ session: name, since: String(since), epoch: t.epoch || "" });
    const r = await Promise.race([(async () => {
      const res = await fetch(`/api/timeline?${q}`, { signal: controller.signal });
      if (!res.ok) throw new Error(String(res.status));
      return res.json();
    })(), cancelled]);
    if (!valid()) return;
    if (controller.signal.aborted) throw new Error("Timeline read cancelled");   // the deadline hit together with the reply
    if (r.epoch !== epoch || !Number.isFinite(r.rev) || r.rev < Math.max(since, want) || !Array.isArray(r.items) ||
        typeof r.full !== "boolean" || !t.epoch && !r.full ||
        r.items.some((it) => !it || !Number.isFinite(it.k) || typeof it.kind !== "string")) {
      throw new Error("Invalid timeline generation");
    }
    t.items = r.full ? r.items.slice(-LIMIT) : merge(t.items, r.items, name);
    t.older = r.older;   // whether anything exists before what was served — if not, no "Show earlier" is drawn
    t.epoch = r.epoch;
    t.rev = r.rev;
    t.error = false;
    t.fails = 0;
    t.nextAt = 0;
    (S.snap.timelines ||= {})[name] = t.items;
  } catch {
    // network, server failure, deadline hit or a reply for the wrong feed — retry after the Fibonacci pause (backoff.js),
    // or at once when the connection is back or the status is pressed, not in a loop: the earlier retry after 300 ms
    // hammered the server endlessly while the feed hung on "Loading feed…". t.again is left alone: a press in the
    // middle of this fetch is one more fetch
    if (valid()) {
      t.error = true;
      if (!t.epoch) failedOnce(t, () => { if (valid() && shown(name)) sync(name); });
    }
  } finally {
    clearTimeout(timeout);
    t.cancel = null;
    t.busy = false;
    // what came during the fetch: a press goes in 300 ms, a hub event waits out the pause of a feed that did not load
    if (valid() && t.again) {
      t.again = false;
      if (!paused(t)) {
        clearTimeout(t.timer);
        t.timer = setTimeout(() => { t.timer = null; if (valid()) sync(name); }, 300);
      }
    }
  }
  if (valid()) renderLive();
}
