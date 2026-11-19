// Small DOM and formatting helpers. User text goes in only through textContent.
import { locale, t, CLOSED } from "./i18n.js";
export const $ = (id) => document.getElementById(id);

export function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

// the chevron of a heading or a badge: "▾" opened, CLOSED shut. An opened one carries data-down="1" from the first build:
// the Clean theme turns it by that mark with a transition, and a node built without it (clean.js sets it a frame later)
// would turn again on every redraw; reconcile.js also replaces a kept badge whose markup differs from the live one
export function chevron(open, cls = "chev") {
  const c = el("span", cls, open ? "▾" : CLOSED);
  if (open) c.dataset.down = "1";
  return c;
}

// a button of the "Waiting for you" strip: disabled while the click is being handled
export function btn(label, onClick, cls = "") {
  const b = el("button", "btn small " + cls, label);
  b.type = "button";
  b.addEventListener("click", async () => { b.disabled = true; try { await onClick(); } finally { b.disabled = false; } });
  return b;
}

// host's children are exactly nodes, in their order, but a node that is already in place is not detached:
// replaceChildren detaches and reattaches every one, and a detached node loses focus (a plugin's field in a page slot
// lost its cursor on every snapshot)
export function placeNodes(host, nodes) {
  const want = new Set(nodes);
  for (const c of [...host.childNodes]) if (!want.has(c)) c.remove();
  let at = host.firstChild;
  for (const n of nodes) {
    if (n === at) at = at.nextSibling;
    else host.insertBefore(n, at);
  }
}

// which look the page is in (main.js mergeFeed, dock.js placeBuilt keep the nodes whose markup did not change, and a theme
// marks the nodes it shows without taking its marks off — another look puts them in anew): the classes a look puts on
// <html> and a count of theme switches; not the passing classes (an open image, a view transition)
let looks = 0;
addEventListener("hub-theme", () => { looks++; });
export const lookSig = () => looks + " " + [...document.documentElement.classList].filter((c) => /^th-|^(glass|has-art|art-plates)$/.test(c)).join(" ");

// a node's markup without the text the page's clock rewrites by itself (main.js tick: the stopwatch, a running step, "ago"):
// a kept node is compared by its markup, and the ticking text is always newer on the page than in a build
const TICKED = /(<[^>]*(?: class="[^"]*\b(?:elapsed|step-run|ago)\b[^"]*"| data-next="[^"]*")[^>]*>)[^<]*/g;
export const unticked = (html) => html.replace(TICKED, "$1");
// the page's own node, not one inside a rendered text (Markdown, its diagrams): whatever finds the page's nodes by a class
// or data-* (the clocks, the feed's scroll anchor) adds it — a diagram's label keeps its data-* and classes, and the
// clock rewrote such a label, or the scroll held on it
export const OWN = ":not(.md *)";

// theme kit (js/themes.js, theme API 1.3): own icons and images instead of session emoji — {icons: {name: url}, avatars: {emoji: url}}
export const kit = { icons: {}, avatars: {} };

// a session emoji — or the theme's image in its place
export function avatarNode(emoji, cls = "avatar") {
  const url = kit.avatars[emoji];
  if (!url) {
    const t = el("span", cls, emoji);
    t.dataset.emoji = emoji;   // a theme adjusts the optical center of a specific emoji: [data-emoji="👍"]
    return t;
  }
  const s = el("span", cls + " kit-img");
  s.style.backgroundImage = `url("${url}")`;
  s.setAttribute("role", "img");
  s.setAttribute("aria-label", emoji);
  return s;
}

// Icons8 icons (the m_outlined pack, the list is in icons8.json) from the /static/icons.svg sprite; a theme's own icon takes over
// alt — the icon to take when the theme has no own name (a theme-only icon such as the quote mark)
export function icon(name, alt) {
  if (alt && !kit.icons[name]) name = alt;
  if (kit.icons[name]) {
    const k = el("span", "icon kit-img");
    k.style.backgroundImage = `url("${kit.icons[name]}")`;
    k.setAttribute("aria-hidden", "true");
    return k;
  }
  const ns = "http://www.w3.org/2000/svg";
  const s = document.createElementNS(ns, "svg");
  s.setAttribute("class", "icon");
  s.setAttribute("aria-hidden", "true");
  const u = document.createElementNS(ns, "use");
  u.setAttribute("href", (document.getElementById("hub-icon-sprite") ? "#i-" : "/static/icons.svg#i-") + name);
  s.append(u);
  return s;
}

// dates are drawn for every feed and list row on every snapshot: toLocale*String builds a formatter for each call (the
// most expensive part of a row), these are made once a minute — the text is the same, and a formatter keeps the time zone
// it was made in (the computer moved to another zone: in a minute the times follow)
const FMT = { hm: { hour: "2-digit", minute: "2-digit" }, dm: { day: "2-digit", month: "2-digit" },
  dmhm: { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" } };
let fmts = null, fmtsAt = 0;
const fmt = (k) => {
  if (!fmts || Date.now() - fmtsAt > 60000) {
    fmts = Object.fromEntries(Object.entries(FMT).map(([n, o]) => [n, new Intl.DateTimeFormat(locale, o)]));
    fmtsAt = Date.now();
  }
  return fmts[k];
};
// today — the time, earlier — the day and month
export const dayTime = (ts) => {
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString() ? fmt("hm").format(d) : fmt("dm").format(d);
};
export const time = (ts) => fmt("hm").format(new Date(ts * 1000));
export const kb = (n) => (n >= 1048576 ? t("web.dom.size_mb", { value: (n / 1048576).toFixed(1) })
  : t("web.dom.size_kb", { value: Math.max(1, Math.round(n / 1024)) }));
export const tok = (n) => (n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n || 0));

// the "when" of a message: every message carries a relative time (5 minutes ago, an hour ago, a day ago)
export function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return t("web.time.just_now");
  if (s < 3600) return t("web.time.minutes_ago", { count: Math.floor(s / 60) });
  if (s < 86400) return t("web.time.hours_ago", { count: Math.floor(s / 3600) });
  const d = new Date(ts * 1000), y = new Date(Date.now() - 86400000);
  if (d.toDateString() === y.toDateString()) return t("web.time.yesterday_at", { time: time(ts) });
  const n = Math.floor(s / 86400);
  return t("web.time.days_ago", { count: n });
}

// a timestamp: "12 min ago", on hover — the date and time; it refreshes itself (main.js)
export function stamp(ts) {
  const s = document.createElement("span");
  s.className = "ago";
  s.dataset.ts = ts;
  s.textContent = ago(ts);
  s.title = fmt("dmhm").format(new Date(ts * 1000));
  return s;
}

export function elapsed(since) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - (since || 0)));
  if (s < 60) return t("web.dom.elapsed_s", { s });
  if (s < 3600) return t("web.dom.elapsed_min", { m: Math.floor(s / 60), s: s % 60 });
  return t("web.dom.elapsed_h", { h: Math.floor(s / 3600), m: Math.floor(s / 60) % 60 });
}

// "Bash step running 6 min 3 s": a running step next to a line not read yet; it ticks by itself (main.js, .step-run)
// head — at the start of a sentence (the strip row): the first letter capitalized, not lowercase as in the middle of one
export function stepNode(step, head = false) {
  const n = el("span", "step-run", stepText(step.name, step.ts, head));
  n.dataset.since = step.ts;
  n.dataset.tool = step.name || "";
  if (head) n.dataset.head = "1";
  return n;
}
export const stepText = (tool, since, head = false) => (head
  ? t("web.dom.step_running_head", { tool: tool || "", elapsed: elapsed(since) })
  : t("web.dom.step_running", { tool: tool || "", elapsed: elapsed(since) }));

export function plural(n, one, few, many) {
  const n10 = n % 10, n100 = n % 100;
  if (n10 === 1 && n100 !== 11) return one;
  if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return few;
  return many;
}

export const SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏";
export const REASONS = {
  "unknown address": t("web.dom.reason_unknown_address"), "session closed": t("web.dom.reason_session_closed"),
  "session failed": t("web.dom.reason_session_failed"), "session removed": t("web.dom.reason_session_removed"),
  "not confirmed after 4 attempts": t("web.dom.reason_not_confirmed"), "removed by owner": t("web.dom.reason_removed"),
  "blocked by hook": t("web.dom.reason_hook_blocked"), "the owner is back": t("web.dom.reason_owner_back"),
  "stopped by owner": t("web.dom.reason_stopped_owner"), "session stopped": t("web.dom.reason_stopped"),
};
export const CHANNELS = { web: t("web.dom.channel_web"), cli: t("web.dom.channel_cli"), terminal: t("web.dom.channel_terminal"),
  session: t("web.dom.channel_session"), telegram: "Telegram", core: t("web.dom.channel_core"), hub: t("web.dom.channel_hub") };
export const RISKS = { money: t("web.dom.risk_money"), prod: t("web.dom.risk_prod"), external: t("web.dom.risk_external"),
  "owner-data": t("web.dom.risk_owner_data"), code: t("web.dom.risk_code") };
