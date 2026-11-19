// Session browsers: local (started by the core itself) and remote (any CDP address). Display only: the hub hands them out.
import { el, time } from "./dom.js";
import { S } from "./state.js";
import { t } from "./i18n.js";

const KIND = { local: t("web.browsers.kind_local"), remote: t("web.browsers.kind_remote") };
const RISK = { "owner-data": t("web.browsers.risk_owner_data"), money: t("web.browsers.risk_money"), prod: t("web.browsers.risk_prod"),
  external: t("web.browsers.risk_external") };

function row(b) {
  const li = el("li", "brow");
  li.dataset.s = b.up ? "active" : "paused";
  const st = el("div", "when");
  st.append(el("span", "t", b.up ? "●" : "○"), el("span", "rel", b.up ? t("web.browsers.responding") : b.idle ? t("web.browsers.idle") : t("web.browsers.not_responding")));
  const body = el("div", "what");
  const title = el("p", "bname");
  title.append(el("b", null, b.name), document.createTextNode(` · ${KIND[b.kind] || b.kind}`));
  const meta = el("div", "meta");
  meta.append(el("span", "id", b.url || ""), document.createTextNode(b.up ? ` · ${b.version}` : ""));
  if (b.since && !b.idle) meta.append(document.createTextNode(" · " + (b.up ? t("web.browsers.responding_since", { time: time(b.since) })
    : t("web.browsers.not_responding_since", { time: time(b.since) }))));
  body.append(title, meta);
  if (b.problem) body.append(el("div", "why", b.problem));   // up with a problem: Chrome answers, its MCP cannot start
  if (b.idle) body.append(el("div", "why muted", t("web.browsers.idle_note")));
  for (const r of b.risk || []) body.append(el("div", "why muted", RISK[r] || r));
  const users = el("div", "meta");
  if (b.users.length) {
    users.append(document.createTextNode(t("web.browsers.connected")));
    b.users.forEach((u, i) => {
      const a = el("a", null, u === "hub" ? t("web.browsers.hub") : u);
      a.href = "#/s/" + encodeURIComponent(u);
      users.append(...(i ? [document.createTextNode(", "), a] : [a]));
    });
  } else {
    users.textContent = t("web.browsers.no_sessions", { tool: b.tool });
  }
  body.append(users);
  li.append(st, body);
  return li;
}

// The "Browsers" section of the settings page
export function browsersSection() {
  const list = S.snap.browsers || [];
  const sec = el("section", "settings-sec");
  sec.setAttribute("aria-labelledby", "sec-browsers");
  const h = el("h3", "day", t("web.browsers.title"));
  h.id = "sec-browsers";
  sec.append(h, el("p", "hint", t("web.browsers.hint")));
  if (!list.length) {
    sec.append(el("p", "hint", t("web.browsers.none")));
  } else {
    const ul = el("ul", "jobs");
    ul.append(...list.map(row));
    sec.append(ul);
  }
  return sec;
}

export const browsersDown = () => (S.snap.browsers || []).filter((x) => !x.up && !x.idle).length;
