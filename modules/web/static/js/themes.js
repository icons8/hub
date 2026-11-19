// The core's own themes (1.36): Clean — the look of the page when this browser has chosen nothing, light or dark as in the
// system (core:clean) or fixed (core:clean-light, core:clean-dark); Classic (core:classic) — the look of the core before
// Clean, light or dark as in the system. Clean's colors and font are in clean/palette.css, its layout in clean/clean.css,
// its page code in clean/clean.js: all of it acts only under the th-clean class on <html> (th-clean-dark — the dark one),
// which only this file sets. A built-in theme is saved as {id}; any other theme lies over Classic.
// A theme's own settings (1.37) are ticks its code declares, hub.addOptions({ themes }) (ext.js): they stand under the row of
// the chosen theme and nowhere else; a theme JSON carries none (nothing to act on them).
// Page themes come from plugins: a plugin ships the theme JSON and lists it in its manifest ("themes": ["nord.json"]);
// your own go as files in themes/ of the hub's settings folder (~/.config/hub-core/themes/ for the main hub). A theme is values of the tokens.css variables over :root:
//   {"name": "Nord", "dark": true, "colors": {"background": "#2e3440", "foreground": "#d8dee9", "primary": "#88c0d0", …}}
// background, foreground and primary are required; the rest are derived from them. The choice is stored in this
// browser and applied before the first render, without flicker.
// Art — a picture next to the theme:
//   "art": {"image": "city.jpg", "veil": 0.8, "position": "center 30%", "glass": true, "credit": "Photo: ..."}
//   art.plates: true — under each feed block a plate in the card color (busy art, the text stays readable);
//   1.3: "icons": {"account": "i/account.png", "clock": "i/clock.png"...} — own icons instead of the standard ones;
//   "avatars": {"🛠️": "a/wrench.png"...} — images instead of session emoji; "font": "f/pixel.woff2" — titles and names;
//   1.8: "font_scope": "all" — the theme font for the whole page (feed, lists, code; "titles" — as without the field),
//   "font_scale": 1.2 — its scale 0.8-1.5 (size-adjust): a pixel font at the same px is smaller. Both only together with "font"
//   1.9: "font_scope": "mono" — the theme font only where the system theme uses monospace (code, paths, feed);
//   "smooth": true — the theme images are not pixel art, smooth them; avatars also replace the page's marks (⏰ 🔔 ⛔)
//   1.36: "measure": "110ch" — the longest line of the feed text and of the rendered text in "Files" (--measure, 68ch in
//   the system theme; 40-200ch, anything else is ignored)
// It goes as the page background under a veil of the background color (veil is its density, 0.5-0.95: the feed text
// stays readable); glass — translucent panels with blur. The server provides the image address (/api/themes/art).
import { el, kit } from "./dom.js";
import { render } from "./state.js";
import { findPlugins } from "./plugins.js";
import { themeOptions, optionValue, setOption, setChosenTheme } from "./ext.js";
import { tickRow } from "./settingrow.js";
import { t } from "./i18n.js";
import { STEPS, savedScale, setTextScale } from "./textscale.js";
import { kitFaces, probe as probeFaces } from "../clean/faces.js";

const KEY = "hub.theme";
export const DEFAULT = "core:clean";
const BUILTIN = { "core:clean": "auto", "core:clean-light": "light", "core:clean-dark": "dark", "core:classic": "" };
export const builtIn = (id) => Object.hasOwn(BUILTIN, id || "");
// the Clean plugin of the catalog became the core's theme (the core no longer loads it, web/server.py IN_CORE): the theme
// chosen with it and its checkboxes move over; the same table is in files/static/theme.js
const MOVED = { "kalimullin:clean:clean.json": "core:clean-light", "kalimullin:clean:clean-dark.json": "core:clean-dark" };
const MOVED_OPTIONS = ["hub.opt.kalimullin:clean", "hub.opt.core:clean"];   // main.js registers Clean as core:clean
const CLEAN_ICONS = ["send", "attach", "mic", "search", "settings", "menu", "allow", "deny", "close", "stop", "clock", "info",
  "board", "browser", "flow", "usage", "chevron", "copy", "reply", "quote"];
const darkSystem = matchMedia("(prefers-color-scheme: dark)");
const REQUIRED = ["background", "foreground", "primary"];
const KEYS = ["background", "foreground", "card", "muted", "muted-2", "muted-foreground", "accent", "border", "input",
  "primary", "primary-foreground", "ring", "destructive", "warning", "success", "code", "code-bg", "hl-attr", "attention"];

function hex(c) {
  const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c || "");
  if (!m) return null;
  const h = m[1].length === 3 ? [...m[1]].map((x) => x + x).join("") : m[1];
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
}
function mix(a, b, t) {   // a → b by the fraction t
  const x = hex(a), y = hex(b);
  if (!x || !y) return `color-mix(in oklch, ${a} ${Math.round((1 - t) * 100)}%, ${b})`;
  return "#" + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, "0")).join("");
}

export function validate(th) {
  if (!th || typeof th !== "object" || typeof th.name !== "string" || !th.name.trim()) return t("web.themes.err_no_name");
  if (!th.colors || typeof th.colors !== "object") return t("web.themes.err_no_colors");
  for (const k of REQUIRED) if (!th.colors[k]) return t("web.themes.err_required", { key: k });
  for (const [k, v] of Object.entries(th.colors)) {
    if (!KEYS.includes(k)) return t("web.themes.err_unknown", { key: k });
    if (typeof v !== "string" || !CSS.supports("color", v)) return t("web.themes.err_color", { key: k, value: v });
  }
  if (th.art !== undefined) {
    const a = th.art;
    if (!a || typeof a !== "object" || typeof a.image !== "string") return t("web.themes.err_art_image");
    if (a.veil !== undefined && !(typeof a.veil === "number" && a.veil >= 0.5 && a.veil <= 0.95)) return t("web.themes.err_veil");
    if (a.position !== undefined && !/^[a-z0-9 %.-]{1,40}$/i.test(String(a.position))) return t("web.themes.err_position");
    if (a.url !== undefined && !String(a.url).startsWith("/api/themes/art?")) return t("web.themes.err_art_url");
  }
  // 1.3: own icons, images instead of session emoji, a title font — files next to the theme
  for (const key of ["icons", "avatars"]) {
    const v = th[key];
    if (v !== undefined && (typeof v !== "object" || !v || Object.values(v).some((x) => typeof x !== "string"))) return t("web.themes.err_object", { key });
  }
  if (th.font !== undefined && typeof th.font !== "string") return t("web.themes.err_font");
  if (th.font_scope !== undefined && !["all", "titles", "mono"].includes(th.font_scope)) return t("web.themes.err_font_scope");
  if (th.font_scale !== undefined && !(typeof th.font_scale === "number" && th.font_scale >= 0.8 && th.font_scale <= 1.5))
    return t("web.themes.err_font_scale");
  if ((th.font_scope !== undefined || th.font_scale !== undefined) && th.font === undefined)
    return t("web.themes.err_font_together");
  for (const u of [...Object.values(th.icons_url || {}), ...Object.values(th.avatars_url || {}), th.font_url || "/api/themes/file?"])
    if (!String(u).startsWith("/api/themes/file?")) return t("web.themes.err_file_url");
  return null;
}

export function complete(theme) {
  const c = { ...theme.colors }, bg = c.background, fg = c.foreground;
  const d = (k, v) => { if (!c[k]) c[k] = v; };
  d("card", mix(bg, fg, 0.04)); d("muted", mix(bg, fg, 0.08)); d("muted-2", mix(bg, fg, 0.12));
  d("muted-foreground", mix(fg, bg, 0.4)); d("accent", c.muted); d("border", mix(bg, fg, 0.15));
  d("input", mix(bg, fg, 0.25)); d("primary-foreground", bg); d("ring", c.primary);
  d("destructive", theme.dark ? "#ff6b6b" : "#d93036"); d("warning", theme.dark ? "#f5c26b" : "#b7791f");
  d("success", theme.dark ? "#6cc287" : "#2f855a"); d("code", c.primary); d("hl-attr", c.primary);
  return c;
}

function applyKit(theme) {
  kit.icons = { ...(theme?.icons_url || {}) };
  kit.avatars = { ...(theme?.avatars_url || {}) };
  let st = document.getElementById("theme-font");
  if (theme?.font_url) {
    if (!st) { st = document.createElement("style"); st.id = "theme-font"; document.head.append(st); }
    const scale = theme.font_scale ? ` size-adjust: ${Math.round(theme.font_scale * 100)}%;` : "";
    st.textContent = `@font-face { font-family: "hub-theme-font"; src: url("${theme.font_url}"); font-display: swap;${scale} }`;
    // 1.9 mono: the theme font applies only to monospace text (code, paths, feed), titles stay as in the system theme
    if (theme.font_scope === "mono") document.documentElement.style.removeProperty("--font-display");
    else document.documentElement.style.setProperty("--font-display", `"hub-theme-font", var(--font)`);
  } else {
    st?.remove();
    document.documentElement.style.removeProperty("--font-display");
  }
  // 1.8: for the whole page — the :root.theme-font-all rule in base.css
  document.documentElement.classList.toggle("theme-font-all", !!theme?.font_url && theme.font_scope === "all");
  document.documentElement.classList.toggle("theme-font-mono", !!theme?.font_url && theme.font_scope === "mono");
  document.documentElement.classList.toggle("theme-smooth", !!theme?.smooth);   // 1.9: the theme images are smoothed
}

// 1.36: "<number>ch" from 40 to 200 (a longer line cannot be read, a shorter one is a column); the same check is in files/theme.js
export function measureOf(theme) {
  const m = /^(\d{2,3}(?:\.\d)?)ch$/.exec(theme?.measure);
  return m && m[1] >= 40 && m[1] <= 200 ? `${m[1]}ch` : null;
}

function reset() {
  const root = document.documentElement, s = root.style;
  for (const k of [...KEYS, "outline-image", "art-image", "art-veil", "art-position", "measure"]) s.removeProperty(`--${k}`);
  s.removeProperty("color-scheme");
  root.classList.remove("has-art", "glass", "art-plates", "th-clean", "th-clean-dark");
}

// a built-in theme: Classic is the page without a theme; Clean is its classes, its icons (Forma, masked by clean.js to
// the text color), its characters instead of the session emoji they stand for (clean/faces.js) and the page's marks ⏰ 🔔 ⛔
// as its images. quiet — before the first render: nothing is drawn to redraw
// (a hub-theme then would render whatever S.snap holds at that moment)
let probed = false;
let shownBuiltIn = "";   // the built-in theme on screen ("" — another one): a broken saved theme shows Clean, its id stays saved
function applyBuiltIn(id, quiet = false) {
  const mode = BUILTIN[id], dark = mode === "dark" || (mode === "auto" && darkSystem.matches);
  shownBuiltIn = id;
  reset();
  applyKit(null);
  if (mode) {
    const root = document.documentElement, dir = `/static/clean/${dark ? "id" : "i"}/`;
    root.classList.add("th-clean");
    root.classList.toggle("th-clean-dark", dark);
    kit.icons = { ...Object.fromEntries(CLEAN_ICONS.map((n) => [n, `${dir}${n}.svg`])), account: `${dir}settings.svg` };
    kit.avatars = { ...kitFaces(), "⏰": `${dir}clock.svg`, "🔔": `${dir}bell.svg`, "⛔": `${dir}noentry.svg` };
    // the characters do not load (an older page server, no network): the emoji and letters stay, once
    if (!probed) { probed = true; probeFaces(() => { if (shownBuiltIn === id) applyBuiltIn(id); }); }
  }
  if (!quiet) window.dispatchEvent(new CustomEvent("hub-theme"));
}
// Clean as in the system follows it without a reload
darkSystem.addEventListener("change", () => { if (BUILTIN[shownBuiltIn] === "auto") applyBuiltIn(shownBuiltIn); });

function apply(theme) {   // a plugin's or own theme
  const root = document.documentElement, s = root.style;
  shownBuiltIn = "";
  reset();
  applyKit(theme);
  if (!theme) { window.dispatchEvent(new CustomEvent("hub-theme")); return; }
  const c = complete(theme), art = theme.art?.url ? theme.art : null;
  if (art?.glass) c.card = `color-mix(in oklch, ${c.card} 72%, transparent)`;
  for (const [k, v] of Object.entries(c)) s.setProperty(`--${k}`, v);
  s.setProperty("color-scheme", theme.dark ? "dark" : "light");
  const measure = measureOf(theme);
  if (measure) s.setProperty("--measure", measure);
  s.setProperty("--outline-image", theme.dark ? "oklch(1 0 0 / 0.1)" : "oklch(0 0 0 / 0.1)");
  if (art) {
    s.setProperty("--art-image", `url("${art.url}")`);
    s.setProperty("--art-veil", `${Math.round((art.veil ?? 0.8) * 100)}%`);
    s.setProperty("--art-position", art.position || "center");
    root.classList.add("has-art");
    root.classList.toggle("glass", !!art.glass);
    root.classList.toggle("art-plates", !!art.plates);   // a backing under the feed text (busy art — the text stays readable)
  }
  window.dispatchEvent(new CustomEvent("hub-theme"));   // canvases ("Flow") recolor themselves
}

function saved() {
  try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch { return null; }
}
const savedId = () => saved()?.id || "";
export const chosenId = () => savedId() || DEFAULT;   // nothing chosen in this browser — Clean
setChosenTheme(chosenId);   // hub.option's default from the chosen theme's group (ext.js)

// the choice made with the Clean plugin of the catalog: to the core's Clean, the checkboxes too (if there are none yet)
function moveChoice() {
  try {
    const to = MOVED[savedId()];
    if (to) localStorage.setItem(KEY, JSON.stringify({ id: to }));
    const [from, into] = MOVED_OPTIONS;
    if (localStorage.getItem(from) && !localStorage.getItem(into)) localStorage.setItem(into, localStorage.getItem(from));
  } catch { /* no storage */ }
}

// before the first render: the chosen theme from this browser's cache; nothing chosen or a broken one — Clean
export function applySavedTheme() {
  moveChoice();
  const t = saved();
  if (builtIn(t?.id)) applyBuiltIn(t.id, true);
  else if (t?.id && !validate(t.theme)) apply(t.theme);
  else applyBuiltIn(DEFAULT, true);
}

// the chosen theme may have been provided by a plugin that is no longer in the catalog (removed or not fully
// installed: the choice stayed in localStorage, and on every load the page requests the theme files and gets 404).
// The check runs no earlier than the first render (applySavedTheme does not wait for the network here), and right
// after it, once the list of themes can be requested from the server.
export function reconcileSavedTheme() {
  if (!savedId() || builtIn(savedId())) return;
  fetch("/api/themes").then((r) => r.json()).then(reconcile).catch(() => {});
}

// the chosen theme against a fresh /api/themes answer: gone altogether (in neither themes nor errors) — unselect it;
// broken (its id is in errors: invalid JSON, an unreadable file) — keep the choice while the file is being fixed;
// changed in the file — apply the fresh one
function reconcile(j) {
  const id = savedId();
  if (!id || builtIn(id) || !Array.isArray(j?.themes)) return;   // the answer is not a catalog (a server error) — unselect nothing
  const now = (j.themes || []).find((t) => t.id === id);
  if (now) {
    if (!validate(now.theme) && localStorage.getItem(KEY) !== JSON.stringify({ id: now.id, theme: now.theme })) choose(now);
  } else if (!(j.errors || []).some((e) => e.id === id)) choose(null);
}

const SCALES = [t("web.themes.scale_normal"), t("web.themes.scale_large"), t("web.themes.scale_xlarge")].map((label, i) => [STEPS[i], label]);

// the #/theme/<id> link applies the theme in this browser and opens "Settings" → "Appearance" (the choice is stored
// in the browser, so it cannot be set from outside any other way)
export function themeFromLink() {
  const m = location.hash.match(/^#\/theme\/(.+)$/);
  if (!m) return;
  const id = MOVED[decodeURIComponent(m[1])] || decodeURIComponent(m[1]);
  localStorage.setItem("hub.settingsTab", "theme");
  history.replaceState(null, "", "#/settings");
  if (builtIn(id)) { choose({ id }); return; }
  fetch("/api/themes").then((r) => r.json()).then((j) => {
    const t = (j.themes || []).find((x) => x.id === id);
    if (t && !validate(t.theme)) choose(t);
  }).catch(() => {});
}

// item: {id, theme} of a plugin's or own theme, {id} of a built-in one; null — nothing chosen (Clean)
function choose(item) {
  if (item && !builtIn(item.id)) {
    localStorage.setItem(KEY, JSON.stringify({ id: item.id, theme: item.theme }));
    apply(item.theme);
    return;
  }
  if (item) localStorage.setItem(KEY, JSON.stringify({ id: item.id }));   // chosen on purpose: stays if the default changes
  else localStorage.removeItem(KEY);
  applyBuiltIn(item?.id || DEFAULT);
}

function textScaleBlock() {
  const box = el("div", "text-scale");
  const seg = el("div", "seg");
  seg.setAttribute("role", "group");
  seg.setAttribute("aria-label", t("web.themes.text_size"));
  const draw = () => { for (const b of seg.children) b.setAttribute("aria-pressed", String(b.dataset.v === savedScale())); };
  for (const [v, label] of SCALES) {
    const b = el("button", null, label);
    b.type = "button";
    b.dataset.v = v;
    b.addEventListener("click", () => {
      setTextScale(v);
      draw();
    });
    seg.append(b);
  }
  draw();
  box.append(el("h3", "day", t("web.themes.text_size")), el("p", "hint", t("web.themes.text_size_hint")), seg);
  return box;
}

// a theme's own settings (hub.addOptions with themes, API 1.37) — the block under the row of the chosen theme; null — it has none
function themeOptionsBlock(id) {
  const groups = themeOptions(id);
  if (!groups.length) return null;
  const box = el("div", "theme-opts");
  box.append(el("p", "hint", t("web.themes.options")));
  for (const g of groups) {
    const list = el("div", "feed-opts");
    list.append(...g.options.map((o) => tickRow(o, (k) => optionValue(g.ext, k), (k, on) => setOption(g.ext, k, on))));
    box.append(...(g.hint ? [el("p", "hint", g.hint)] : []), list);
  }
  return box;
}
const optionsSig = (id) => JSON.stringify([id, themeOptions(id).map((g) => [g.ext, g.sig])]);

// the nearest scrolling box of a node (the settings feed), else the page
function scroller(n) {
  for (let p = n.parentElement; p; p = p.parentElement)
    if (/(auto|scroll)/.test(getComputedStyle(p).overflowY) && p.scrollHeight > p.clientHeight) return p;
  return document.scrollingElement;
}

// "Settings" → "Appearance": the core's themes (Clean, Classic) + plugin themes and your own files; the chosen theme's own
// settings under its row. sync() — after a snapshot or a plugin's addOptions: redraws if they changed, else re-reads the ticks
export function themesSection() {
  const sec = el("section", "settings-sec");
  const list = el("div", "theme-list");
  const more = el("button", "btn quiet small", t("web.themes.find_more"));
  more.type = "button";
  more.addEventListener("click", () => { localStorage.setItem("hub.settingsTab", "plugins"); findPlugins(t("web.themes.find_query")); render(); });
  const intro = el("p", "hint");   // names the folder of this hub's own themes: the answer of /api/themes has it
  intro.hidden = true;
  const showIntro = (folder) => {   // a server that does not name it yet (not restarted after an update): the main hub's
    intro.textContent = t("web.themes.intro", { folder: typeof folder === "string" ? folder : "~/.config/hub-core/themes/" });
    intro.hidden = false;
  };
  sec.append(el("h3", "day", t("web.themes.title")), intro, list, more);
  sec.prepend(textScaleBlock());
  const row = (item, title, sub, colors) => {
    const b = el("button", "theme-row");
    b.type = "button";
    b.dataset.id = item.id;
    b.setAttribute("aria-pressed", String(item.id === chosenId()));
    // a preview on every row: the theme's art, otherwise its background with an accent stripe; a built-in one is drawn
    // by its class (Clean's colors are in clean/palette.css), "as in the system" — half light and half dark
    const pv = el("span", "preview" + (builtIn(item.id) ? ` ${item.id.replace("core:", "")}` : ""));
    if (item.theme?.art?.url) pv.style.backgroundImage = `url("${item.theme.art.url}")`;
    else if (item.theme) { pv.style.background = colors[0]; const bar = el("span", "bar"); bar.style.background = colors[2]; pv.append(bar); }
    else if (item.id !== "core:classic") pv.append(el("span", "bar"));
    b.append(pv);
    const sw = el("span", "swatches");
    for (const c of colors) { const d = el("span", "sw"); d.style.background = c; sw.append(d); }
    const txt = el("span", "txt");
    txt.append(el("span", "nm", title), el("span", "lb", sub));
    b.append(sw, txt);
    b.addEventListener("click", () => {
      const before = b.getBoundingClientRect().top, focused = document.activeElement === b;
      choose(item);   // hub-theme redraws the list
      if (b.isConnected) draw();
      // the settings block went from one row to another: the pressed row stays where it was on the screen, with the focus
      const now = [...list.children].find((r) => r.dataset.id === item.id);
      if (!now) return;
      scroller(now).scrollTop += now.getBoundingClientRect().top - before;
      if (focused) now.focus({ preventScroll: true });
    });
    return b;
  };
  let got = { themes: [], errors: [] }, drawnSig = "";
  const draw = () => {
    const rows = [row({ id: "core:clean" }, t("web.themes.clean"), t("web.themes.clean_sub"), []),
      row({ id: "core:classic" }, t("web.themes.classic"), t("web.themes.classic_sub"), []),
      row({ id: "core:clean-light" }, t("web.themes.clean_light"), t("web.themes.light"), []),
      row({ id: "core:clean-dark" }, t("web.themes.clean_dark"), t("web.themes.dark"), [])];
    for (const it of got.themes) {
      const bad = validate(it.theme);
      if (bad) { rows.push(el("p", "hint warn", `${it.origin}: ${bad}`)); continue; }
      const c = complete(it.theme);
      const r = row(it, it.theme.name, [it.theme.dark ? t("web.themes.dark") : t("web.themes.light"), it.theme.art?.url && t("web.themes.with_art"), it.origin]
        .filter(Boolean).join(" · "), [c.background, c.foreground, c.primary, c.success, c.warning, c.destructive]);
      if (it.theme.art?.credit) r.querySelector(".txt").append(el("span", "lb", String(it.theme.art.credit).slice(0, 160)));
      if (it.warning) r.querySelector(".txt").append(el("span", "lb warn", it.warning));
      rows.push(r);
    }
    for (const e of got.errors) rows.push(el("p", "hint warn", `${e.origin} (${e.id}): ${e.error}`));
    const id = chosenId(), at = rows.findIndex((r) => r.dataset?.id === id), opts = at < 0 ? null : themeOptionsBlock(id);
    if (opts) rows.splice(at + 1, 0, opts);
    drawnSig = optionsSig(id);
    list.replaceChildren(...rows);
  };
  sec.sync = () => {
    if (optionsSig(chosenId()) !== drawnSig) draw();
    else for (const l of list.querySelectorAll(".theme-opts label.opt")) l.sync?.();
  };
  draw();
  window.addEventListener("hub-theme", () => draw());   // the theme was applied by a link — mark it in the list
  fetch("/api/themes").then((r) => r.json()).then((j) => {
    got = j;
    reconcile(j);
    draw();
    showIntro(j.folder);
  }).catch(() => showIntro());
  return sec;
}
