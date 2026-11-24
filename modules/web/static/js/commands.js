// Commands typed with "/" in the session input field. Two kinds:
//   core commands — /compact, /reload, /model … — the page does not type them into the session as text but records
//     the same fact for the core as the button or `hub session compact`: the core knows a compaction is under way and
//     does not confuse it with "idle, but a message is pending" (a text /compact could get stuck, the watchdog
//     raised an alarm, the hub compacted a second time);
//   plugin commands — a plugin declares them itself (hub.addCommand, API 1.13), the page calls its run without the model.
// Only what the core and plugins know is executed from the list; any other "/name" (a skill, a foreign command) goes
// to the session as plain text, just as without the command popup. A path (/Users/…) is not a command either and
// also goes as text.
import { post } from "./api.js";
import { confirmDlg } from "./dialogs.js";
import { S } from "./state.js";
import { t } from "./i18n.js";

const NAME = /^[a-z0-9][a-z0-9-]*$/;
// what the core accepts in /model and /effort — from the snapshot (core/catalog)
const lists = () => S.snap?.commands || {};
const codexHub = () => lists().codex === true;   // a hub entirely on Codex: its models are codex/<id>

// the page server's response: an error becomes an exception with its text (the field gets the typed text back); if the
// core did not record it in time, say so (queued) rather than "accepted"
async function call(path, body, ok) {
  const r = await post(path, body);
  if (r.error) throw new Error(r.error);
  return r.data?.queued || r.data?.text ? r.data.text : ok;
}

// the same list for clients outside the page — commands.list of the snapshot (server.py COMMANDS; a test checks the names)
const CORE = [
  // the core decides when to compact and restart; progress shows as lines in the feed ("compacting", "compacted",
  // "restarted")
  { name: "compact", args: t("web.cmd.compact_args"), hint: t("web.cmd.compact_hint"), run: (a, c) => call("/api/session/compact",
    { session: c.name, ...(a ? { text: a } : {}) }, t("web.cmd.compact_ok")) },
  { name: "reload", hint: t("web.cmd.reload_hint"), run: (_, c) => call("/api/session/reload", { session: c.name }, t("web.cmd.reload_ok")) },
  { name: "model", args: t("web.cmd.model_args"), hint: t("web.cmd.model_hint"), choices: () => lists().models,
    // a full claude-… id too; a hub on Codex (its models are codex/<id>) — any codex/<id>, as the core accepts
    need: (a) => (lists().models || []).includes(a) || (codexHub() ? /^codex\/[A-Za-z0-9][\w.:-]{0,79}$/ : /^claude-[\w.[\]-]+$/).test(a),
    run: (a, c) => call("/api/session/model", { session: c.name, model: a }, t("web.cmd.model_ok", { name: c.name, value: a })) },
  { name: "effort", args: t("web.cmd.effort_args"), hint: t("web.cmd.effort_hint"), choices: () => [...(lists().efforts || []), "default"],
    need: (a) => [...(lists().efforts || []), "default"].includes(a),   // default — clear the session's own level (it is not in the snapshot)
    run: (a, c) => call("/api/session/effort", { session: c.name, level: a }, t("web.cmd.effort_ok", { name: c.name, value: a })) },
  { name: "stop", hint: t("web.cmd.stop_hint"), run: (_, c) => call("/api/interrupt", { session: c.name }, t("web.cmd.stop_ok")) },
  // specs for this session (core/specmode.py): only a person turns them on, here or in Telegram
  { name: "spec", args: t("web.cmd.spec_args"), hint: t("web.cmd.spec_hint"), choices: () => ["on", "off"],
    need: (a) => ["on", "off"].includes(a),
    run: (a, c) => call("/api/session/spec", { session: c.name, on: a === "on" },
      t(a === "on" ? "web.cmd.spec_on" : "web.cmd.spec_off", { name: c.name })) },
  { name: "away", args: t("web.cmd.away_args"), hint: t("web.cmd.away_hint"), run: (a) => {
    // /away [hours] [deputies [<person> …]]: the deputies' areas go to them (core/deputies.py)
    const words = a.split(/\s+/).filter(Boolean), at = words.findIndex((w) => w.toLowerCase() === "deputies");
    const flag = at < 0 ? null : (words.slice(at + 1).length ? words.slice(at + 1) : true);
    const h = at < 0 ? words : words.slice(0, at);
    const hours = h.length ? Number(h[0].replace(",", ".")) : null;
    if (h.length > 1 || (h.length && !(hours > 0))) throw new Error(t("web.cmd.away_bad"));
    return call("/api/away", { ...(hours ? { hours } : {}), ...(flag ? { deputies: flag } : {}) },
      hours ? t("web.cmd.away_for", { hours: h[0] }) : t("web.cmd.away_ok"));
  } },
  { name: "back", hint: t("web.cmd.back_hint"), run: () => call("/api/owner/back", {}, t("web.cmd.back_ok")) },
  { name: "restart", args: t("web.cmd.restart_args"), hint: t("web.cmd.restart_hint"), run: (a) => {
    const parts = a.split(/\s+/).filter(Boolean);
    if (parts.length) return call("/api/restart", { parts }, t("web.cmd.restart_parts", { parts: parts.join(", ") }));
    return new Promise((done, fail) => {
      confirmDlg(t("web.cmd.restart_title"), t("web.commands.restart_text"), t("web.cmd.restart_btn"),
      () => call("/api/restart", { parts: [] }, t("web.cmd.restart_ok")).then(done, fail), "primary");
      document.getElementById("confirm-dlg")?.addEventListener("close", (e) => {
        if (e.target.returnValue !== "ok") done(t("web.cmd.restart_cancelled"));
      }, { once: true });
    });
  } },
].map((c) => ({ args: "", ...c, by: t("web.cmd.by_core") }));
const CORE_NAMES = new Set(CORE.map((c) => c.name));

const plugins = new Map();   // name -> {name, args, hint, run, by, ext}

// hub.addCommand (API 1.13): an error is returned as text (the plugin sees it under "Plugins"), otherwise null
export function addCommand(ext, cmd) {
  const name = String(cmd?.name || "").replace(/^\//, "");
  if (!NAME.test(name) || typeof cmd.run !== "function") return t("web.cmd.bad_name");
  if (CORE_NAMES.has(name)) return t("web.cmd.core_taken", { name });
  const had = plugins.get(name);
  if (had && had.ext !== ext) return t("web.cmd.taken", { name, ext: had.ext });
  plugins.set(name, { name, args: String(cmd.args || ""), hint: String(cmd.hint || ""), run: cmd.run, ext,
    by: String(cmd.by || ext.split(":").pop()) });
  return null;
}

// "/name arguments" → {name, args}; a path (/Users/…) and plain text give null
export function parse(text) {
  const m = /^\/([A-Za-z0-9][\w:.-]*)(?:\s+([\s\S]*))?$/.exec(String(text || "").trim());
  return m ? { name: m[1], args: (m[2] || "").trim() } : null;
}

// what to do with the typed text: {kind: "core"|"plugin", cmd, args} | {kind: "none"} (not in the list) | null (not a command)
export function resolve(text) {
  const p = parse(text);
  if (!p) return null;
  const name = p.name.toLowerCase();
  if (CORE_NAMES.has(name)) return { kind: "core", cmd: CORE.find((c) => c.name === name), args: p.args };
  if (plugins.has(name)) return { kind: "plugin", cmd: plugins.get(name), args: p.args };
  return { kind: "none" };
}

// suggestions while typing "/": core commands, then plugins, by name prefix; once a space is typed there are none
export function suggest(text) {
  const m = /^\/([\w-]*)$/.exec(text);
  if (!m) return [];
  const q = m[1].toLowerCase();
  return [...CORE, ...plugins.values()].filter((c) => c.name.startsWith(q));
}

// execute: returns a result string for the hint under the field; an error is an exception with text for the owner
export async function run(r, session) {
  const c = r.cmd;
  // extra text after a command without arguments is not dropped silently (/stop urgent) — the typed text returns to the field
  if (r.kind === "core" && !c.args && r.args) throw new Error(t("web.cmd.no_args", { name: c.name }));
  const one = c.choices ? t("web.cmd.one_of", { list: (c.choices() || []).join(", ") }) : "";
  if (c.args.startsWith("<") && !r.args) throw new Error(`/${c.name} ${c.args}${one}`);
  if (c.need && !c.need(r.args)) throw new Error(`/${c.name} ${c.args}${one}`);
  const out = await c.run(r.args, { name: session?.name, session });
  return typeof out === "string" ? out : t("web.cmd.done", { name: c.name });
}
