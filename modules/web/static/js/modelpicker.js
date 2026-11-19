// A session's model and reasoning level under the input field: the model by its full name ("Opus 5.5"), a click opens
// a menu above the row with the current choice marked; the choice goes through the existing /api/session/model and
// /api/session/effort (the session is restarted with the same conversation).
import { S, isRunner, render } from "./state.js";
import { moreMenu } from "./menu.js";
import { banner, post } from "./api.js";
import { t } from "./i18n.js";
import { el } from "./dom.js";

const changing = new Set();   // "model|effort" + the session whose change is still waiting for the core's answer: a second choice is not sent

// id → a human-readable name: claude-opus-5-5[-YYYYMMDD] → "Opus 5.5", the old form claude-3-5-sonnet → "Sonnet 3.5",
// the [1m] suffix → "(1M)"; gpt-... → uppercase; an alias and anything else — capitalized
function parse(id) {
  const wide = /\[1m\]$/i.test(id), base = id.replace(/\[1m\]$/i, "");
  const m = /^claude-([a-z]+)-(\d+)(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(base);
  const old = /^claude-(\d+)(?:-(\d{1,2}))?-([a-z]+)(?:-\d{8})?$/.exec(base);
  const [fam, major, minor] = m ? m.slice(1) : old ? [old[3], old[1], old[2]] : [];
  return fam && { family: fam[0].toUpperCase() + fam.slice(1), version: major + (minor ? "." + minor : ""), wide };
}
// a hub entirely on Codex: its models are codex/<id> in the snapshot with their names (commands.titles);
// the session reports the bare id it runs on
const codexTitle = (id) => S.snap?.commands?.titles?.[id.startsWith("codex/") ? id : "codex/" + id];
export function modelTitle(id) {
  if (id === "default") return t("web.dock.model_default");
  const cx = codexTitle(id);
  if (cx) return cx;
  if (id.startsWith("codex/")) id = id.slice(6) || id;
  const p = parse(id);
  if (p) return `${p.family} ${p.version}${p.wide ? " (1M)" : ""}`;
  if (/^[a-z]+\[1m\]$/i.test(id)) return id[0].toUpperCase() + id.slice(1, -4) + " (1M)";
  if (/^gpt-[\w.-]+$/.test(id)) return id.toUpperCase();
  return id[0].toUpperCase() + id.slice(1);
}
const shortTitle = (id) => parse(id)?.family || modelTitle(id).replace(/ \(1M\)$/, "");   // narrow — "Opus"

// what to caption the button with: the model the session is running on, while it is also the chosen one (or nothing
// is chosen); if another was chosen and the session has not restarted yet — the chosen one, otherwise the menu and the
// button disagree
function shown(s) {
  const own = s.model || "default", run = s.info?.model;
  const same = run === own || "codex/" + run === own   // a Codex session reports the bare id
    || parse(run || "")?.family.toLowerCase() === own.replace(/\[1m\]$/i, "");   // an alias matches by family
  return run && (own === "default" || same) ? run : own;
}
export const modelLabel = (s) => modelTitle(shown(s));

// items for choosing one of the values, with the chosen one marked; the choice is post(path, {session, ...}); until the
// core answers, this choice's button is disabled and a second choice is not sent
function options(s, { key, own, ids, title, path, body }) {
  const choose = async (id) => {
    if (id === own || changing.has(key + s.name)) return;
    changing.add(key + s.name);
    render();
    const r = await post(path, { session: s.name, ...body(id) });
    changing.delete(key + s.name);
    if (r.error) banner(r.error);
    else if (r.data?.queued) banner(r.data.text);
    render();
  };
  return ids.map((id) => ({ label: title(id), checked: id === own, onClick: () => choose(id) }));
}

// the button under the input field: a caption (shorter when narrow), a menu above the row, focus on the marked item
function button(s, { key, cls, items, label, short, change, aria }) {
  const m = moreMenu(key + ":" + s.name, items, label, { up: true, focusChecked: true });
  m.classList.add(cls);
  const b = m.querySelector(".menu-btn");
  b.className = "linkbtn menu-btn";
  b.replaceChildren(el("span", "mod-long", label), el("span", "mod-short", short));
  b.disabled = changing.has(key + s.name);
  b.title = change;
  b.setAttribute("aria-label", aria);
  return m;
}

// reasoning level: its own (s.effort) — until the session has restarted with it, the chosen one is captioned; without
// its own — the one the core started the session with (info.effort), marked "Default"; before the first start —
// just "effort"
export const effortLabel = (s) => s.effort || s.info?.effort || t("web.dock.effort_unset");
const effortHead = (s) => ({ text: t("web.dock.effort_now", { effort: effortLabel(s) }) });
// the levels the session's model knows (a hub on Codex: commands.model_efforts; elsewhere every level); its own level
// stays listed even when the model does not know it — the menu shows what is set
function efforts(s) {
  const all = S.snap?.commands?.efforts || [], per = S.snap?.commands?.model_efforts;
  const known = per && (per[s.model || "default"] || (s.info?.model && per["codex/" + s.info.model]));
  return [...new Set([...(known ? all.filter((e) => known.includes(e)) : all), ...(s.effort ? [s.effort] : []), "default"])];
}
// dflt — the "default" caption: the model menu has its own "Default" next to it, the level has its own
const effortOptions = (s, dflt) => options(s, { key: "effort", own: s.effort || "default",
  title: (id) => id === "default" ? dflt : id, ids: efforts(s),
  path: "/api/session/effort", body: (id) => ({ level: id }) });

// the levels are also a section in the model menu (class mod-eff): on a narrow screen there is no level button, and the
// style shows the section only there — so the level can be changed from a phone too
export function modelControl(s) {
  // the choice is only s.model; info.model merely captions the button and the marked row
  const own = s.model || "default", now = shown(s), label = modelTitle(now);
  const ids = [...new Set([...(S.snap?.commands?.models || []), "default"])];
  if (!ids.includes(own)) ids.splice(-1, 0, own);   // the full id as its own row — before "Default"
  const items = options(s, { key: "model", own, ids, title: (id) => id === own && own !== "default" ? label : modelTitle(id),
    path: "/api/session/model", body: (id) => ({ model: id }) });
  if (isRunner(s)) items.unshift({ text: t("web.dock.model_executor_note") }, { sep: true });
  items.push(...[{ sep: true }, effortHead(s), ...effortOptions(s, t("web.dock.effort_default"))].map((it) => ({ ...it, cls: "mod-eff" })));
  const m = button(s, { key: "model", cls: "stat-mod", items, label, short: shortTitle(now),
    change: t("web.dock.model_change"), aria: t("web.dock.model_aria", { model: label }) });
  m.querySelector(".menu-btn").classList.add("stat-model");
  return m;
}

export function effortControl(s) {
  const label = effortLabel(s);
  return button(s, { key: "effort", cls: "stat-eff", items: [effortHead(s), { sep: true }, ...effortOptions(s, t("web.dock.model_default"))],
    label, short: label, change: t("web.dock.effort_change"), aria: t("web.dock.effort_aria", { effort: label }) });
}
