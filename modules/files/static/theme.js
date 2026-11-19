// Theme of "Files" (hub theme plugins): the hub's theme choice lives in localStorage of its page (its own port); "Files" is another
// origin and cannot see it. The same theme list (/api/themes, served by modules/web) and the same choice format {id, theme} as in
// web/js/themes.js, but in its own localStorage; applied before the first paint (a plain script in <head>, not a module:
// the hub's themes.js drags in the state and plugins of its page). ?theme=<id> (or #...&theme=<id>) picks and
// remembers a theme, ?theme=system forgets the choice. The header has a picker: the core's themes + the others.
// The core's themes as on the page (1.36): nothing chosen — Clean, light or dark as in the system (core:clean), or fixed
// (core:clean-light, core:clean-dark); Classic (core:classic) — the look before Clean. Clean here is its colors and font,
// /web/clean/palette.css under html.th-clean (th-clean-dark — the dark one); a built-in theme is saved as {id}.
(() => {
  const KEY = "files.theme";
  const KEYS = ["background", "foreground", "card", "muted", "muted-2", "muted-foreground", "accent", "border", "input",
    "primary", "primary-foreground", "ring", "destructive", "warning", "success", "code", "code-bg", "hl-attr", "attention"];
  const root = document.documentElement;
  const DEFAULT = "core:clean";
  const BUILTIN = { "core:clean": "auto", "core:clean-light": "light", "core:clean-dark": "dark", "core:classic": "" };
  const builtIn = (id) => Object.hasOwn(BUILTIN, id || "");
  // the Clean plugin of the catalog became the core's theme: the choice made with it moves over (as in web/js/themes.js)
  const MOVED = { "kalimullin:clean:clean.json": "core:clean-light", "kalimullin:clean:clean-dark.json": "core:clean-dark" };
  const darkSystem = matchMedia("(prefers-color-scheme: dark)");
  // "Text size" of the hub page (its Settings → Look): the page leaves the choice in a cookie named by this "Files" port, which the
  // ports of one host share (this localStorage is another one); without it — the default step of tokens.css
  const scale = new RegExp("(?:^|; )hub\\.textScale\\." + location.port + "=([\\d.]{1,6})(?:;|$)").exec(document.cookie);
  if (scale && scale[1] >= 0.8 && scale[1] <= 2) root.style.setProperty("--ui-scale", String(Number(scale[1])));

  function hex(c) {
    const m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(c || "");
    if (!m) return null;
    const h = m[1].length === 3 ? [...m[1]].map((x) => x + x).join("") : m[1];
    return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16));
  }
  function mix(a, b, t) {  // a → b by fraction t, as in the hub, so the shades match
    const x = hex(a), y = hex(b);
    if (!x || !y) return `color-mix(in oklch, ${a} ${Math.round((1 - t) * 100)}%, ${b})`;
    return "#" + x.map((v, i) => Math.round(v + (y[i] - v) * t).toString(16).padStart(2, "0")).join("");
  }
  // missing colors come from the background, text and accent (web/js/themes.js complete)
  function complete(theme) {
    const c = { ...theme.colors }, bg = c.background, fg = c.foreground;
    const d = (k, v) => { if (!c[k]) c[k] = v; };
    d("card", mix(bg, fg, 0.04)); d("muted", mix(bg, fg, 0.08)); d("muted-2", mix(bg, fg, 0.12));
    d("muted-foreground", mix(fg, bg, 0.4)); d("accent", c.muted); d("border", mix(bg, fg, 0.15));
    d("input", mix(bg, fg, 0.25)); d("primary-foreground", bg); d("ring", c.primary);
    d("destructive", theme.dark ? "#ff6b6b" : "#d93036"); d("warning", theme.dark ? "#f5c26b" : "#b7791f");
    d("success", theme.dark ? "#6cc287" : "#2f855a"); d("code", c.primary); d("hl-attr", c.primary);
    return c;
  }
  // "measure": "<number>ch" from 40 to 200 — the width of the text line (--measure); the same check as in web/js/themes.js
  const measureOf = (theme) => {
    const m = /^(\d{2,3}(?:\.\d)?)ch$/.exec(theme.measure);
    return m && m[1] >= 40 && m[1] <= 200 ? `${m[1]}ch` : null;
  };
  // the hub page validates the shape; here it only keeps a malformed theme from breaking the page
  const ok = (t) => !!(t && t.colors && ["background", "foreground", "primary"].every((k) => t.colors[k]) &&
    Object.entries(t.colors).every(([k, v]) => KEYS.includes(k) && typeof v === "string" && CSS.supports("color", v)));

  function apply(theme) {   // a plugin's or own theme; null — none (applyBuiltIn starts with it)
    const s = root.style;
    shownBuiltIn = "";
    for (const k of [...KEYS, "outline-image", "art-image", "art-veil", "art-position", "font-display", "measure"]) s.removeProperty(`--${k}`);
    s.removeProperty("color-scheme");
    root.classList.remove("has-art", "glass", "art-plates", "theme-font-all", "theme-font-mono", "th-clean", "th-clean-dark");
    document.getElementById("theme-font")?.remove();
    if (!theme) return;
    const c = complete(theme), art = theme.art && theme.art.url ? theme.art : null;
    if (art && art.glass) c.card = `color-mix(in oklch, ${c.card} 72%, transparent)`;
    for (const [k, v] of Object.entries(c)) s.setProperty(`--${k}`, v);
    s.setProperty("color-scheme", theme.dark ? "dark" : "light");
    const measure = measureOf(theme);
    if (measure) s.setProperty("--measure", measure);
    s.setProperty("--outline-image", theme.dark ? "oklch(1 0 0 / 0.1)" : "oklch(0 0 0 / 0.1)");
    if (art) {  // art goes behind the shell under a veil (base.css html.has-art)
      s.setProperty("--art-image", `url("${art.url}")`);
      s.setProperty("--art-veil", `${Math.round((art.veil ?? 0.8) * 100)}%`);
      s.setProperty("--art-position", art.position || "center");
      root.classList.add("has-art");
      root.classList.toggle("glass", !!art.glass);
    }
    if (theme.font_url) {  // theme font: headings; "all" is the whole page, "mono" is code and paths (base.css)
      const st = document.createElement("style");
      st.id = "theme-font";
      const scale = theme.font_scale ? ` size-adjust: ${Math.round(theme.font_scale * 100)}%;` : "";
      st.textContent = `@font-face { font-family: "hub-theme-font"; src: url("${theme.font_url}"); font-display: swap;${scale} }`;
      document.head.append(st);
      if (theme.font_scope !== "mono") s.setProperty("--font-display", `"hub-theme-font", var(--font)`);
      root.classList.toggle("theme-font-all", theme.font_scope === "all");
      root.classList.toggle("theme-font-mono", theme.font_scope === "mono");
    }
  }

  let shownBuiltIn = "";   // the built-in theme on screen: a broken saved theme shows Clean, its id stays saved
  function applyBuiltIn(id) {
    const mode = BUILTIN[id];
    apply(null);
    shownBuiltIn = id;
    root.classList.toggle("th-clean", !!mode);
    root.classList.toggle("th-clean-dark", mode === "dark" || (mode === "auto" && darkSystem.matches));
  }

  const saved = () => { try { return JSON.parse(localStorage.getItem(KEY) || "null"); } catch { return null; } };
  const chosenId = () => saved()?.id || DEFAULT;   // nothing chosen in this browser — Clean
  // item: {id, theme} of a plugin's or own theme, {id} of a built-in one; null — nothing chosen (Clean)
  function choose(item) {
    try {
      if (item) localStorage.setItem(KEY, JSON.stringify(builtIn(item.id) ? { id: item.id } : { id: item.id, theme: item.theme }));
      else localStorage.removeItem(KEY);
    } catch { /* no storage: the theme lasts only until reload */ }
    if (item && !builtIn(item.id)) apply(item.theme);
    else applyBuiltIn(item?.id || DEFAULT);
  }
  function applySaved() {
    const was = saved();
    if (builtIn(was?.id)) applyBuiltIn(was.id);
    else if (was?.id && ok(was.theme)) apply(was.theme);
    else applyBuiltIn(DEFAULT);
  }

  // before the first paint: the theme chosen in this browser
  try { if (MOVED[saved()?.id]) localStorage.setItem(KEY, JSON.stringify({ id: MOVED[saved().id] })); } catch { /* no storage */ }
  applySaved();
  darkSystem.addEventListener("change", () => { if (BUILTIN[shownBuiltIn] === "auto") applyBuiltIn(shownBuiltIn); });

  // ?theme=<id> in the address or in the tail #...?...&theme=<id>: pick and remember it, remove it from the address
  function fromLink() {
    const search = new URLSearchParams(location.search);
    const [hash, query] = location.hash.split("?");
    const hq = new URLSearchParams(query || "");
    const id = search.get("theme") ?? hq.get("theme");
    if (id == null) return null;
    search.delete("theme");
    hq.delete("theme");
    history.replaceState(null, "", location.pathname + (search.size ? "?" + search : "") + hash + (hq.size ? "?" + hq : ""));
    return id;
  }
  const wanted = fromLink();
  if (wanted === "" || wanted === "system") choose(null);
  else if (builtIn(MOVED[wanted] || wanted)) choose({ id: MOVED[wanted] || wanted });   // no list needed: at once

  // picker in the header: the core's themes + the others; the list comes from the server, a fresh version of the chosen theme is applied
  function picker(themes, t) {
    const box = document.getElementById("themes");
    if (!box) return;
    const sel = document.createElement("select");
    sel.className = "theme-pick";
    sel.setAttribute("aria-label", t("files.theme"));
    sel.title = t("files.theme_title");
    const add = (value, label) => { const o = document.createElement("option"); o.value = value; o.textContent = label; sel.append(o); };
    add("core:clean", t("web.themes.clean"));
    add("core:classic", t("web.themes.classic"));
    add("core:clean-light", t("web.themes.clean_light"));
    add("core:clean-dark", t("web.themes.clean_dark"));
    for (const t of themes) add(t.id, t.theme.name);
    sel.value = chosenId();
    if (sel.value !== chosenId()) sel.value = DEFAULT;  // the chosen theme no longer exists
    sel.addEventListener("change", () => choose(builtIn(sel.value) ? { id: sel.value } : themes.find((t) => t.id === sel.value) || null));
    box.replaceChildren(sel);
  }

  const list = fetch("/api/themes").then((r) => r.json())
    .then((j) => (j.themes || []).filter((t) => ok(t.theme) && typeof t.theme.name === "string")).catch(() => []);
  // theme from the link, otherwise a fresh version of the chosen one (changed in the file: apply the new one)
  const sync = (themes, id) => {
    id = MOVED[id] || id;
    if (builtIn(id)) { if (id !== saved()?.id) choose({ id }); }
    else {
      const now = themes.find((t) => t.id === id);
      if (now && JSON.stringify({ id: now.id, theme: now.theme }) !== JSON.stringify(saved())) choose(now);
    }
    const sel = document.querySelector("#themes select");
    if (sel) sel.value = chosenId();
  };
  list.then(async (themes) => {
    sync(themes, wanted && wanted !== "system" ? wanted : saved()?.id);
    // a plain script (before styles), not a module: page strings are loaded by dynamic import, when the picker is drawn
    const { t } = await import("/web/js/i18n.js");
    const draw = () => picker(themes, t);
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", draw); else draw();
  });
  // a link with a theme inside an already open page (#...?theme=): no reload
  window.addEventListener("hashchange", () => {
    const id = fromLink();
    if (id === "" || id === "system") { choose(null); list.then((themes) => sync(themes, null)); }
    else if (id != null) list.then((themes) => sync(themes, id));
  });
})();
