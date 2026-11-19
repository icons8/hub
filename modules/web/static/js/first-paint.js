// The look chosen in this browser, set before the first paint: a plain script in <head>, not a module (main.js and themes.js
// run after the page is drawn, and the whole page moved into Clean's layout under the reader: a layout shift of 0.56 on a
// desktop, 0.9 on a phone). Only Clean's classes and the text size; themes.js applies the rest (icons, a plugin's theme)
// as before. The tables mirror themes.js (BUILTIN, MOVED) and textscale.js (STEPS); "Files" does the same in its theme.js
(() => {
  const root = document.documentElement;
  const BUILTIN = { "core:clean": "auto", "core:clean-light": "light", "core:clean-dark": "dark", "core:classic": "" };
  const MOVED = { "kalimullin:clean:clean.json": "core:clean-light", "kalimullin:clean:clean-dark.json": "core:clean-dark" };
  const STEPS = ["1.0667", "1.2", "1.3333"], OLD = { "1": "1.0667", "1.125": "1.2", "1.25": "1.3333" };
  let id = "", scale = "";
  try {
    id = JSON.parse(localStorage.getItem("hub.theme") || "null")?.id || "";
    scale = localStorage.getItem("hub.textScale") || "";
  } catch { /* no storage: the defaults */ }
  id = MOVED[id] || id || "core:clean";   // nothing chosen — Clean
  const mode = Object.hasOwn(BUILTIN, id) ? BUILTIN[id] : undefined;   // undefined — a plugin's theme: themes.js applies it
  if (mode) {
    const dark = mode === "dark" || (mode === "auto" && matchMedia("(prefers-color-scheme: dark)").matches);
    root.classList.add("th-clean");
    root.classList.toggle("th-clean-dark", dark);
  }
  scale = OLD[scale] || scale;
  if (STEPS.includes(scale)) root.style.setProperty("--ui-scale", scale);
})();
