// Page strings in the hub language (i18n/<lang>.json, served as /i18n.json; core/i18n.py does the same for Python).
// A key missing in the language falls back to English; missing in English too — the key itself (tests see it). The
// server picks the language: hub.json → owner language → for a hub from before languages — Russian → browser →
// English (core/i18n.py human). The server already renders the static text of index.html in that language
// (data-i18n="key", data-i18n-<attribute>="key").
// If the strings are not available (the server is restarting; an old server with new page code returns 404), the page
// modules wait for them here, retrying with a pause of up to 5 s instead of rendering keys until a manual reload: while
// waiting, the page the server already rendered in its language is visible, with its own "no connection" banner
// (data-wait on #banner).
async function load() {
  for (let pause = 250, tries = 0; ; pause = Math.min(pause * 2, 5000), tries++) {
    try {
      const r = await fetch("/i18n.json", { credentials: "same-origin" });
      if (r.ok) return await r.json();
      if (!tries) console.warn(`/i18n.json: ${r.status}`);   // the server answered but gave no strings — visible in the console
    } catch {}
    const b = document.getElementById("banner");
    if (b && tries && b.dataset.wait) { b.textContent = b.dataset.wait; b.classList.add("on"); }
    await new Promise((done) => setTimeout(done, pause));
  }
}
const data = await load();
const banner = document.getElementById("banner");
if (banner && banner.textContent && banner.textContent === banner.dataset.wait) { banner.textContent = ""; banner.classList.remove("on"); }

export const lang = data.lang;
export const dir = data.dir;
// the chevron of a closed group points at the line end; the arrow between a sender and a recipient follows the text
export const CLOSED = dir === "rtl" ? "◂" : "▸";
export const TO = dir === "rtl" ? "←" : "→";
// locale for browser dates and numbers (toLocaleString): English for the pseudo-language xx
export const locale = lang === "xx" ? "en" : lang;
const rules = new Intl.PluralRules(locale);
const fallbackRules = new Intl.PluralRules("en");

// a right-to-left page: each substituted value is isolated (first strong isolate … pop), so an English name or a "?" at
// its end keeps its place inside the sentence instead of jumping to the other side. The marks are for the screen only:
// what the page sends or copies drops them (bare — api.js, the "Copy" of a code block)
const iso = dir === "rtl" ? (v) => `\u2068${v}\u2069` : (v) => v;
export const isolate = iso;
export const bare = (text) => (typeof text === "string" ? text.replace(/[\u2066-\u2069]/g, "") : text);

function fill(text, params) {
  return text.replace(/\{\{|\}\}|\{(\w+)\}/g, (m, name) => (!name ? m[0] : name in params ? iso(String(params[name])) : m));
}

// text for a key; params are {name} substitutions, count selects the plural form (the language's CLDR)
export function t(key, params = {}) {
  let value = data.messages[key], r = rules;
  if (value == null) { value = data.fallback[key]; r = fallbackRules; }
  if (value == null) return key;
  if (typeof value === "object") value = value[r.select(params.count ?? 0)] ?? value.other ?? key;
  return fill(value, params);
}

// whether the key exists (for core text with a key — show the translation, otherwise the text as is)
export const has = (key) => key in data.messages || key in data.fallback;
