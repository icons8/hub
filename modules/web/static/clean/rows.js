// The "Detailed list" of sessions (clean.js rowDetail): the pure parts — what comes from where in the snapshot, testable without a browser.
// Data — only what the core already returns in the sessions snapshot: out_head (start of the last reply; if absent — gist.text), since,
// state, cwd (for the tooltip).

// time label: a working session — "now" (its since is the start of the turn, not the last activity); otherwise — time since the last message (lastAt)
// words: the page's strings (clean.js, web.clean.ago_*); AGO — English, when there are none
export const AGO = { now: "now", m: (n) => `${n}m`, h: (n) => `${n}h`, d: (n) => `${n}d` };
export function agoText(s, now, words = AGO) {
  if (s.state === "busy" || s.state === "starting") return words.now;
  const sec = Math.max(0, now - (lastAt(s) || now));   // the same time the list is sorted by
  if (sec < 60) return words.now;
  return sec < 3600 ? words.m(Math.floor(sec / 60)) : sec < 86400 ? words.h(Math.floor(sec / 3600)) : words.d(Math.floor(sec / 86400));
}

// last message preview: the core returns the start of the session's reply in `out_head` (≈160 chars, line breaks already spaces; executors
// that never wrote to the chat have none — then `gist.text`, the gist of the turn) —
// markup is stripped, leaving plain text for one line; empty — no second line
export function flat(text, max = 120) {
  const t = String(text || "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")          // image
    .replace(/\[([^\]]*)\]\([^)]*\)/g, "$1")          // link → its text
    .replace(/^\s{0,3}(#{1,6}|>)\s*/gm, "")             // heading, quote
    .replace(/```|\*\*|__|~~|`/g, "")                  // code and emphasis
    .replace(/(^|\s)[-*•]\s+/g, "$1")                   // list marker, glued to a space where a line break was
    .replace(/\s+/g, " ").trim().replace(/^HANDOFF-[A-Z]+:?\s*/, "")   // service tag of a task handoff between sessions — not text for the list
    .replace(/[…\s]+$/, "");
  return t.length > max ? t.slice(0, max) : t;
}

// the session of a row by the name in it: the core shows an executor's name under its parent without the prefix (app-dev-api → api)
export function sessionFor(name, list) {
  if (!name) return null;
  const exact = list.find((s) => s.name === name);
  if (exact) return exact;
  const tails = list.filter((s) => s.name.endsWith("-" + name));
  return tails.length === 1 ? tails[0] : null;
}

// the same lookup for a whole list at once: every row asks for its session on every redraw, so the names are put into maps once
// per sessions array (the core hands out a new array only when the sessions changed) instead of a scan of all sessions per row.
// find(name) answers exactly as sessionFor(name, list); byName — the first session with that exact name, as Array.find
export function sessionIndex(list) {
  const byName = new Map(), tails = new Map();
  for (const s of list) {
    if (!byName.has(s.name)) byName.set(s.name, s);
    for (let i = s.name.indexOf("-"); i >= 0; i = s.name.indexOf("-", i + 1)) {
      const t = s.name.slice(i + 1);
      tails.set(t, tails.has(t) ? null : s);   // two sessions end the same way: no guess
    }
  }
  return { byName, find: (name) => (name ? byName.get(name) || tails.get(name) || null : null) };
}

// the second line of a session that works or waits itself: what it says it does (its gist, set in this turn — newer than the start of the
// turn, `since`) while it works, else the core's caption of the row (`caption`: what it does now, what it waits for and from whom) — it
// stays with a wait too. "Itself" — a turn, a launch, background work, a question to the owner, a `hub waiting`; a parent that is free
// and spins only for its executors is not (its icon says so, its line is the start of its last result). "" — not such a session
export function liveLine(s, caption) {
  if (!s) return "";
  const own = s.state === "busy" || s.state === "starting";
  // the watchdog's "reply?" is "Waiting for you" in "Now": its line is the action (the caption), not the result
  const waits = s.status === "waiting" || s.state === "dialog" || (s.state === "idle" && !!s.waiting) || !!s.attention;
  if (s.compacting && !waits) return caption || "";   // "Compacting the conversation…": the caption the core drew, not the last result
  if (s.mcp_wait?.until > Date.now() / 1000 && s.state === "idle" && !waits) return caption || "";   // "Waiting for MCP…": a free "idle" would be untrue
  if (!waits && !own && !(s.state === "idle" && (s.background || []).length)) return "";
  const gist = own && !waits && s.gist?.text && s.gist.ts >= (s.since || 0) ? s.gist.text : "";
  return gist || caption || "";
}

// what to draw in the row: preview, time, full path for the tooltip; null — the row stays compact. `said` — liveLine of the session:
// it replaces the start of the last result, which a working or waiting session has not got (or has an old one)
export function detailOf(s, now, words, said = "") {
  if (!s) return null;
  // an unread answer to the owner ("Answered you" in "Now") shows the answer itself, not a later result sent on its own
  // (the same rule as answered() and leadLine in js/sidebar.js)
  const reply = s.unread && (s.owner_reply || 0) > (s.seen || 0) && s.owner_reply !== s.owner_out ? s.reply_head : "";
  const prev = flat(said || reply || s.out_head || s.gist?.text);
  if (s.kind === "hub") return { prev, ago: "", cwd: "" };   // "Hub": a preview like everyone's; it is always online — no time, its own tooltip
  return { prev, ago: agoText(s, now, words), cwd: s.cwd || "" };
}

// when a chat last had a message, in snapshot time: `since` is when the session took its current state (a turn the owner started,
// the end of a turn, the moment it fell asleep) and `out_head_ts` is when its last result for the owner arrived; the later one wins.
// The snapshot has no time of the owner's own last line, but a message to a session starts its turn, which moves `since`
export const lastAt = (s) => Math.max(s?.since || 0, s?.out_head_ts || 0);

// Telegram order for one group: the freshest chat on top, the hub (kind "hub") stays first; equal times keep the order given
export function byRecent(rows) {
  return rows.map((r, i) => [r, i]).sort((a, b) => (b[0].kind === "hub") - (a[0].kind === "hub") || lastAt(b[0]) - lastAt(a[0]) || a[1] - b[1]).map((x) => x[0]);
}
