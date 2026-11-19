// "Text size" (Settings → Look): 1.0667 (default, = --ui-base in tokens.css) / 1.2 / 1.3333 as the --ui-scale multiplier on <html>:
// every font of the page is --text-* (themed by it) or, in Clean, a fixed size times its ratio to the default (--c-rel in
// clean.css); a plugin theme does not touch it (KEYS are colors only). The previous version stored 1 / 1.125 / 1.25 (1 =
// default): they are mapped to the nearest step. The labels of the steps are in themes.js (the Look tab)
import { S } from "./state.js";

export const SCALE_KEY = "hub.textScale";
export const DEFAULT_SCALE = "1.0667";
export const STEPS = [DEFAULT_SCALE, "1.2", "1.3333"];
const OLD_SCALES = { "1": "1.0667", "1.125": "1.2", "1.25": "1.3333" };

export function savedScale() {
  let v = DEFAULT_SCALE;
  try { v = localStorage.getItem(SCALE_KEY) || DEFAULT_SCALE; } catch { /* none */ }
  v = OLD_SCALES[v] || v;
  return STEPS.includes(v) ? v : DEFAULT_SCALE;
}

// "Files" is another origin (its own port): it cannot read this localStorage, but a cookie is shared by the ports of one host,
// so the choice also goes there (files/static/theme.js reads it). The cookie is named by that hub's "Files" port: two hubs on
// one host do not overwrite each other's; the default step is no cookie. Written once the snapshot says which port it is
let shared = "";
export function shareTextScale() {
  const port = S.snap?.files_port, v = savedScale();
  if (!port || shared === `${port} ${v}`) return;
  shared = `${port} ${v}`;
  const name = `${SCALE_KEY}.${port}`;
  try { document.cookie = `${name}=${v === DEFAULT_SCALE ? ";max-age=0" : v + ";max-age=31536000"};path=/;SameSite=Lax`; } catch { /* none */ }
}

export function applySavedTextScale() {   // before the first render, without flicker
  document.documentElement.style.setProperty("--ui-scale", savedScale());
  shareTextScale();
}

export function setTextScale(v) {
  try { if (v === DEFAULT_SCALE) localStorage.removeItem(SCALE_KEY); else localStorage.setItem(SCALE_KEY, v); } catch { /* none */ }
  applySavedTextScale();   // at once, without a reload
}
