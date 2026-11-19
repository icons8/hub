// The band "The hub needs an update" at the top of the page (app spec §7.2): the app on a code layer below the
// distribution server's minimum version (snapshot update_check.min, modules/update/layer.py) — to which version, why,
// what stops working below it, and "Update" (the same press as in "About the hub"). A minimum from a later day is the
// same band with its date. No commands for a terminal: the only way out is "Update". A hub from git, an older core or a
// hub at the minimum has no key — no band.
import { el } from "./dom.js";
import { post } from "./api.js";
import { S } from "./state.js";
import { lang, t } from "./i18n.js";

export const minBar = el("section", "minbar");
minBar.setAttribute("aria-label", t("web.updates.min_label"));
minBar.hidden = true;

const STOPS = {
  claude: "web.updates.min_stop_claude", "update-format": "web.updates.min_stop_update_format",
  bus: "web.updates.min_stop_bus", stats: "web.updates.min_stop_stats",
};
let sent = 0, note = "", painted = "";

function reasonOf(low) {
  const r = low.reason && typeof low.reason === "object" ? low.reason : {};
  const mine = r[lang] || r[String(lang).split("-")[0]] || r.en || Object.values(r).find((x) => typeof x === "string");
  return typeof mine === "string" ? mine : "";
}

export function renderMin() {
  const c = S.snap?.update_check, low = c?.layer && c.min && c.min.below === true ? c.min : null;
  // the check after the press says what came of it (installing, restarting, failed): the button waits for it
  const waiting = sent > 0 && !(c && c.ts * 1000 >= sent);
  const key = low ? JSON.stringify([low, waiting, note, c.result]) : "";
  if (key === painted) return;
  painted = key;
  minBar.hidden = !low;
  if (!low) return minBar.replaceChildren();
  const row = el("div", "minbar-row");
  const tx = el("span", "minbar-t");
  tx.setAttribute("role", "status");
  tx.append(el("b", null, low.ahead ? t("web.updates.min_band_ahead", { version: low.version, since: low.since || "" })
    : t("web.updates.min_band", { version: low.version })));
  const why = reasonOf(low);
  if (why) tx.append(" ", why);
  const stops = (Array.isArray(low.stops) ? low.stops : []).filter((s) => STOPS[s]).map((s) => t(STOPS[s]));
  if (stops.length) tx.append(" ", el("span", "minbar-stops", t("web.updates.min_stops", { list: stops.join("; ") })));
  if (note) tx.append(" ", el("span", "minbar-err", note));
  const go = el("button", "btn small primary", waiting ? t("web.updates.min_installing") : t("web.updates.update_app"));
  go.type = "button";
  go.disabled = waiting || c.result === "restarting";
  go.addEventListener("click", async () => {
    sent = Date.now();
    note = "";
    renderMin();
    const r = await post("/api/update/install", {});
    if (r.error) { sent = 0; note = String(r.error); }
    window.dispatchEvent(new Event("hub-about"));
    renderMin();
  });
  row.append(tx, go);
  minBar.replaceChildren(row);
}
