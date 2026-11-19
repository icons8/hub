// Usage block of Clean at the bottom of the sidebar (place sidebar.footer), as in the native app (LimitsBlock):
// bars of the "5 hours" and "week" windows with an even-pace mark, the percent on hover or tap, a compact / expanded switch,
// the account e-mail with a check when it is the active one, and a clock with a hint when the reading is old.
// Bar colour: not working or 95%+ — red, 80–95% — orange, below 80% — grey, or yellow in economy (economy never covers red or orange).
// Everything comes from the core snapshot: limits, account, accounts, budget (limit saving). This file is the pure part (data and
// words): clean.js draws it and passes the words from the catalogs (web.clean.usage_*); tests/clean/usage-bars.test.mjs runs it
// without a browser. The weekly Opus and Sonnet windows, when the core sends them, are extra rows of the expanded view.

export const SPAN = { five_hour: 5 * 3600, seven_day: 7 * 86400, seven_day_opus: 7 * 86400, seven_day_sonnet: 7 * 86400 };
export const ORDER = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"];
const MAIN = ["five_hour", "seven_day"];   // the two bars of the compact view
const HOUR = 3600;

// the English words — what clean.js falls back to when a catalog has no string; {time} is filled in here
export const WORDS = {
  five_hour: "5 hours", seven_day: "Week", seven_day_opus: "Week · Opus", seven_day_sonnet: "Week · Sonnet",
  mark: "even pace", lapsed: "limit reset, no fresh reading", compact: "Compact", expanded: "Expanded", accounts: "Accounts",
  active: "active", soon: "soon", resets: "resets {when}", at: "Measured at {time}",
  link: "No connection to the hub since {time} — showing the last measurement received. It updates by itself once the connection is back",
  cache: "This is a saved copy — the hub has not answered since the page opened. It updates by itself once the connection is back",
  idle: "The account is not active, and the hub measures only the active one. It updates once the hub switches to it",
  rest: "No session has run on this account for over an hour. Usage updates with the next session reply",
};

const clamp = (x) => Math.min(1, Math.max(0, x));
const fill = (s, vars) => s.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? vars[k] : m));

// "in 4 hours": days → hours → minutes, rounded to the nearest whole unit; a rounding that reaches the next unit moves to
// it (23 h 40 min → 1 day). Less than a minute — "soon". As When.left in the native app.
export function left(at, now, locale, words = WORDS) {
  const s = at - now;
  if (!(s >= 60)) return words.soon;
  let n, unit;
  if (s >= 86400) { n = Math.round(s / 86400); unit = "day"; }
  else if (s >= HOUR) { n = Math.round(s / HOUR); unit = "hour"; if (n >= 24) { n = 1; unit = "day"; } }
  else { n = Math.round(s / 60); unit = "minute"; if (n >= 60) { n = 1; unit = "hour"; } }
  try { return new Intl.RelativeTimeFormat(locale || undefined, { numeric: "always" }).format(n, unit); } catch { return `${n} ${unit}`; }
}

// E-mail labels from the longest to the shortest: the whole address, then without the top-level zone
// ("name@company.example.com" → "name@company.example"); if that does not fit either, CSS cuts the end with "…".
export function emailSteps(email) {
  const at = email.lastIndexOf("@"), dot = email.lastIndexOf(".");
  return dot > at + 1 ? [email, email.slice(0, dot)] : [email];
}

const goodMail = (s) => typeof s === "string" && /^\S{1,100}@\S{1,100}$/.test(s);

// Why the reading is old — the link to the hub goes first, then the saved copy, then the account itself.
function ageWhy(active, linkDown, sinceSnap) {
  if (sinceSnap == null) return "cache";
  if (linkDown) return "link";
  return active ? "rest" : "idle";
}

// The block's data from a snapshot, or null when there is nothing to show (no 5-hour or weekly window).
// now and sinceSnap are seconds; sinceSnap — how long ago the last snapshot came (null — none since the page opened).
export function usageModel(snap, { now, words = WORDS, locale, linkDown = false, sinceSnap = 0 } = {}) {
  const limits = snap?.limits || {}, acc = snap?.account || {}, accounts = snap?.accounts || {};
  const current = limits.account || acc.active || "";
  const ids = ORDER.filter((id) => typeof limits.windows?.[id]?.used === "number");
  if (!MAIN.some((id) => ids.includes(id))) return null;
  const eco = snap?.budget?.mode === "economy";   // limit saving: the core sends budget only while a mode is in effect
  const measured = limits.ts || null;
  // old = the reading itself is over an hour old; no link or no snapshot for a while only explains why (ageWhy), it does not
  // make a fresh reading old: the stream sends a snapshot only on a hub event, so a quiet hub or a reconnect blip must not
  // pale the bars and claim the link is down
  const old = measured != null && now - measured > HOUR;
  const broken = limits.status === "rejected";   // the hub refused the account
  const active = !acc.active || current === acc.active;
  const windows = ids.map((id) => {
    const w = limits.windows[id], used = w.used, resets = w.resets ?? w.reset ?? null;
    const lapsed = typeof resets === "number" && resets <= now;
    const pct = Math.round(clamp(used) * 100);
    const even = typeof resets === "number" && !lapsed ? clamp(1 - (resets - now) / SPAN[id]) : null;
    // colour, red first: not working or 95%+ — red, 80–95% — orange, the rest — grey; economy turns only that grey yellow
    // and never covers red or orange. A window whose reset has passed has no colour.
    const tone = lapsed ? "ok" : broken || pct >= 95 ? "high" : pct >= 80 ? "warn" : eco ? "eco" : "ok";
    const reset = lapsed ? "" : typeof resets === "number" ? fill(words.resets, { when: left(resets, now, locale, words) }) : "";
    const spoken = lapsed ? `${words[id]}, ${words.lapsed}` : `${words[id]} ${pct}%${reset ? `, ${reset}` : ""}`;
    return { id, name: words[id], pct, used: clamp(used), even, lapsed, tone, reset, spoken, extra: !MAIN.includes(id) };
  });
  const pool = Array.isArray(accounts.pool) ? accounts.pool : [];
  const mail = pool.find((p) => p?.name === current)?.email;
  const several = !!accounts.on && pool.length > 1;
  const email = several && goodMail(mail) ? mail : "";
  let stale = null;
  if (old) {
    const why = ageWhy(active, linkDown, sinceSnap);
    const clock = (ts) => new Date(ts * 1000).toLocaleTimeString(locale || undefined, { hour: "2-digit", minute: "2-digit" });
    const lines = [];
    if (why !== "cache") lines.push(fill(words.at, { time: clock(measured) }));
    lines.push(fill(words[why], { time: clock(sinceSnap != null ? now - sinceSnap : measured) }));
    stale = { why, text: lines.join("\n") };
  }
  // the account line of the expanded view: the e-mail, or the account name as the core gives it when there is no e-mail
  const title = email || current;
  return {
    account: current, title, email, line: several ? title : "", active: !!acc.active && current === acc.active,
    broken, eco, windows, stale, words, pool: !!accounts.on,
  };
}
