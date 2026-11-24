// Feed participants: who is speaking shows by side, name and hue, not by color alone.
// The hue comes from a hash of the name; the hue values themselves live in the tokens (css/tokens.css, --who-h-0...11)
// and are not duplicated here.
import { avatarNode, el } from "./dom.js";
import { S, sessionName } from "./state.js";
import { t } from "./i18n.js";

// the core's sender codes (core/views.py _WHO; an archive parse of an earlier core is read as them: views.sender_now);
// a session name has no ":", so a session is never taken for one of them
const CORE = ":core", SCHEDULE = ":schedule", OWNER = ":owner";   // OWNER: a message the core makes from the owner's word
const QUIET = new Set([SCHEDULE, CORE]);   // service senders: neutral, no hue
let hues = null;

function readHues() {
  const cs = getComputedStyle(document.documentElement);
  hues = Array.from({ length: 12 }, (_, i) => Number(cs.getPropertyValue(`--who-h-${i}`)) || 250);
}
const hash = (s) => { let h = 5381; for (const ch of s) h = ((h << 5) + h + ch.codePointAt(0)) >>> 0; return h; };

// the hub session is shown as "Hub", as in the list on the left, whatever it is called in the settings
const isHub = (name) => name === "hub" || (S.snap?.sessions || []).some((s) => s.name === name && s.kind === "hub");
// a core module and a plugin process write to sessions as mod:<name> / ext:<name> (core/views.py) — captioned in words, not as an id
const outside = (name) => {
  const s = String(name);
  return s.startsWith("mod:") ? t("web.who.module", { name: s.slice(4) }) : s.startsWith("ext:") ? t("web.who.plugin", { name: s.slice(4) }) : null;
};
export const nameOf = (name) => (isHub(name) ? t("web.who.hub") : String(name) === OWNER ? t("web.who.owner") : outside(name) || String(sessionName(name)));

// name: the session name, "hub" is the hub, the core and schedule names (CORE, SCHEDULE) are service senders, mod:<name> and ext:<name> are a module and a plugin; null is the owner
// (the owner himself; OWNER is the core's retelling of his word — a sender row, named through the catalog)
export function whoOf(name) {
  if (name == null) {
    const label = t("web.who.you");
    return { key: "owner", label, initial: label.charAt(0).toUpperCase(), quiet: false, hue: null, owner: true };
  }
  const hub = isHub(name);
  // the core's service senders — in the page language
  const label = hub ? t("web.who.hub") : String(name) === CORE ? t("web.who.core") : String(name) === SCHEDULE ? t("web.who.schedule")
    : String(name) === OWNER ? t("web.who.owner") : outside(name) || String(sessionName(name));
  if (!hues) readHues();
  return { key: hub ? "hub" : String(name), label, initial: label.charAt(0).toUpperCase(), quiet: QUIET.has(String(name)), hue: hues[hash(hub ? "hub" : String(name)) % 12], owner: false };
}

// a member of the hub's Telegram group (core/views.py: an in row's `member` {name}; a core before it has only from.kind "member",
// the name then is ""): his words are a person's, not the owner's — null for any other row
export const memberOf = (it) => (typeof it?.member?.name === "string" ? it.member.name : it?.from?.kind === "member" ? "" : null);

// the member as a participant: the name (without it — the role), a hue of his own — a session called the same does not share it
export function whoOfMember(name) {
  const label = name || t("web.who.member");
  if (!hues) readHues();
  return { key: `tg:${label}`, label, initial: label.charAt(0).toUpperCase(), quiet: false, hue: hues[hash(`tg:${label}`) % 12], owner: false };
}

// marks a node as "somebody's": the .who class and the --h hue (the tokens derive the color and background from it)
export function paint(node, who) {
  node.classList.add("who");
  if (who.quiet) node.classList.add("quiet");
  else if (who.hue != null) node.style.setProperty("--h", String(who.hue));
  return node;
}

// author name (who — when it is not a session's, see whoOfMember): an avatar letter and the name; at the start of a session's prose turn, and in other participants' messages
export function byline(name, who = whoOf(name)) {
  const b = el("span", "byline");
  // an agent has its own emoji in the list — it is also the avatar; without one, the first letter of the name
  // a member has no session: name is null; "hub" is the hub session whatever it is called
  const all = S.snap?.sessions || [];
  const icon = (all.find((s) => s.name === name) || (name === "hub" ? all.find((s) => s.kind === "hub") : null))?.icon;
  const av = icon ? avatarNode(icon, "av emoji") : el("span", "av", who.initial);
  av.setAttribute("aria-hidden", "true");
  b.append(av, el("b", null, who.label));
  return paint(b, who);
}
