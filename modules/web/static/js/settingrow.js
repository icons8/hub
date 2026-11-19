// A hub setting row (allowlist in modules/web/settings.py) — shared by the "General" tab and the "Modules" cards.
// Kept in its own file: settings.js and modules.js both import it from here, with no circular imports.
import { el } from "./dom.js";
import { post } from "./api.js";
import { S } from "./state.js";
import { t } from "./i18n.js";
import { keepOpen } from "./keepopen.js";

// saves one setting and says how it went in the note next to the field
async function saveSetting(note, key, value) {
  note.textContent = "…";
  const res = await post("/api/settings/set", { key, value });
  note.textContent = res.error ? t("web.updates.failed", { error: res.error }) : t("web.updates.saved");
  // page language: the server renders strings and static text in it — reload (a second is enough for the core to pick up hub.json)
  if (!res.error && key === "language") setTimeout(() => location.reload(), 1500);
  setTimeout(() => { if (note.textContent === t("web.updates.saved")) note.textContent = ""; }, 2500);
}

// A tick of a choice kept in this browser ("Look": the feed, plugin groups, a theme's own settings): o is { key, label,
// hint? }, value(key) reads the choice, set(key, on) keeps it; sync() of the row re-reads it
export function tickRow(o, value, set) {
  const l = el("label", "opt"), box = el("input"), txt = el("span");
  box.type = "checkbox";
  box.checked = value(o.key);
  box.addEventListener("change", () => set(o.key, box.checked));
  txt.append(document.createTextNode(o.label), ...(o.hint ? [el("small", null, o.hint)] : []));
  l.append(box, txt);
  l.sync = () => { box.checked = value(o.key); };
  return l;
}

// One line per agent: the model and the effort of its executors (executors.agents.<agent>.*, the two rows the server
// sends) are two ordinary select rows, each saved under its own key and with its own note, a short caption before
// each list; empty — the kind of work decides. setModel and setEffort show the saved values.
export function agentRow(model, effort) {
  const row = el("div", "set-row set-agent");
  row.dataset.agent = model.agent;
  const same = { "": t("web.settings.executors_agent_same") };
  const [m, e] = [settingRow({ ...model, names: same }, t("web.settings.executors_agent_caption_model")),
    settingRow({ ...effort, names: same }, t("web.settings.executors_agent_caption_effort"))];
  row.append(el("span", "set-label", model.agent), m.node, e.node);
  return { node: row, setModel: m.set, setEffort: e.set };
}

// shown: the text next to the control, the label by default (the aria-label stays the label)
export function settingRow(r, shown = r.label) {
  const note = el("span", "hint set-note");
  const save = (value) => saveSetting(note, r.key, value);
  let control, set;
  if (r.kind === "bool") {
    const l = el("label", "opt");
    const box = el("input");
    box.type = "checkbox";
    box.addEventListener("change", () => save(box.checked));
    const txt = el("span");
    txt.append(document.createTextNode(shown), ...(r.hint ? [el("small", null, r.hint)] : []), note);
    l.append(box, txt);
    set = (v) => { box.checked = !!v; box.disabled = !!r.locked; };
    control = l;
  } else {
    const row = el("label", "set-row");
    const txt = el("span", "set-label");
    txt.append(document.createTextNode(shown), ...(r.hint ? [el("small", null, r.hint)] : []));
    let input;
    if (r.kind === "enum") {
      input = el("select");
      // names — option labels from the server (in its own language); absent — the value as is
      for (const c of r.choices) { const o = el("option", null, r.names?.[c] ?? (c || (r.key.startsWith("executors.") ? t("web.settings.same_as_sessions") : t("web.settings.default")))); o.value = c; input.append(o); }
      input.addEventListener("change", () => save(input.value));
    } else if (r.kind === "tz") {
      // time zone as a list ("auto (zone name)" plus the option to choose): this browser's zone comes first
      input = el("select");
      const auto = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
      const zones = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [auto, "UTC"];
      const first = el("option", null, t("web.settings.tz_auto", { zone: auto }));
      first.value = auto;
      input.append(first, ...zones.map((z) => { const o = el("option", null, z); o.value = z; return o; }));
      input.addEventListener("change", () => save(input.value));
      set = (v) => {   // the saved zone is not in the browser's list (an old name) — add it so it is visible
        if (v && ![...input.options].some((o) => o.value === v && o !== first)) { const o = el("option", null, v); o.value = v; input.append(o); }
        if (document.activeElement !== input) input.value = v && v !== auto ? v : auto;
      };
    } else {
      input = el("input");
      input.type = r.kind === "num" ? "number" : "text";
      if (r.kind === "num") { input.min = r.min; input.max = r.max; }
      if (r.kind === "title") input.maxLength = 40;
      input.addEventListener("change", () => save(r.kind === "num" ? Number(input.value) : input.value));
    }
    input.setAttribute("aria-label", r.label);
    if (input.tagName === "SELECT") input.translate = false;   // the values are the hub's own (model names, effort levels, zones): not for the browser's page translation
    row.append(txt, input, note);
    set ||= (v) => { if (document.activeElement !== input) input.value = v ?? ""; };
    control = row;
  }
  return { node: control, set };
}

// module settings live in its card on the "Modules" tab; the cards are rebuilt with every snapshot, but the settings node
// is one per module (otherwise the "Saved / Failed" result goes to a stale node and is not visible); "advanced" rows
// are collapsed
const boxes = new Map();   // module -> { sig, node, rows: [[key, set]] }
const rowsOf = (id) => (S.snap.settings || []).filter((r) => r.place === "module:" + id);
export const hasModuleSettings = (id) => (S.snap.settings || []).some((r) => r.place === "module:" + id);

export function moduleSettings(id) {
  const rows = rowsOf(id);
  if (!rows.length) { boxes.delete(id); return null; }
  const sig = JSON.stringify(rows.map(({ value, ...r }) => r));   // everything except the value: choices, locked, labels
  let b = boxes.get(id);
  if (!b || b.sig !== sig) {
    const node = el("div"), simple = el("div", "feed-opts"), deep = el("div", "feed-opts"), more = el("details", "set-more");
    keepOpen(more, "module:" + id);   // rebuilt when the module's rows change
    more.append(el("summary", null, t("web.settings.advanced")), deep);
    const made = rows.map((r) => { const f = settingRow(r); (r.advanced ? deep : simple).append(f.node); return [r.key, f.set]; });
    node.append(simple, ...(deep.children.length ? [more] : []));
    b = { sig, node, rows: made };
    boxes.set(id, b);
  }
  const value = new Map(rows.map((r) => [r.key, r.value]));
  for (const [key, set] of b.rows) set(value.get(key));
  return b.node;
}
