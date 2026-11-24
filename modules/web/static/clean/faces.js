// Clean's characters: pictures of sessions (AI-generated, see THIRD-PARTY-NOTICES.md), png files in a/. A session whose icon is one of
// these emoji shows its character instead of the emoji everywhere the page draws the icon (js/themes.js puts them into kit.avatars);
// a specialist the owner gave no icon gets one by its role, a session with no icon one by its name, always the same (clean.js); an emoji of the owner's own stays an emoji
export const DIR = "/static/clean/a/";
// an emoji with and without U+FE0F is one icon on the screen (groups.js bare): the keys here are the bare form, one emoji to a picture
// (the icon picker has one cell per picture: a second emoji would be a cell that is never the chosen one)
export const bare = (e) => e.replace(/️/g, "");
export const FACES = {
  "🤖": "clay-coral-beanie.png", "🧭": "clay-mustard-headset.png", "🧑‍💻": "clay-violet-scarf.png",
  "🔬": "clay-sky-cap.png", "🎨": "clay-tomato-glasses.png", "🧪": "clay-teal-earmuffs.png",
  "🧩": "clay-mint-souwester.png", "🖌": "clay-peach-bandana.png", "✍": "clay-indigo-beret.png",
  "⚙": "clay-bubblegum-sunglasses.png", "🛠": "clay-lemon-party-hat.png", "💬": "clay-sage-goggles.png",
  "📊": "clay-lilac-daisies.png", "🔎": "clay-cocoa-trapper.png", "🚀": "clay-aqua-hard-hat.png",
  "⚡": "clay-ivory-captain.png", "🧊": "clay-plum-crown.png", "🌙": "clay-forest-sweatband.png", "🌊": "clay-slate-bow.png",
  "🎉": "clay-lime-propeller.png", "🐛": "clay-magenta-cat-ears.png", "🎧": "clay-sand-straw-hat.png",
  "🔮": "clay-powder-headlamp.png", "🐜": "clay-cherry-sailor.png"
};
// the pictures in a fixed order: a name takes the one at hash % 24, so the order is the contract, not the order of FACES above
export const PICTURES = [
  "clay-coral-beanie", "clay-mustard-headset", "clay-violet-scarf", "clay-sky-cap", "clay-tomato-glasses", "clay-teal-earmuffs",
  "clay-mint-souwester", "clay-peach-bandana", "clay-indigo-beret", "clay-bubblegum-sunglasses", "clay-lemon-party-hat", "clay-sage-goggles",
  "clay-lilac-daisies", "clay-cocoa-trapper", "clay-aqua-hard-hat", "clay-ivory-captain", "clay-plum-crown", "clay-forest-sweatband",
  "clay-slate-bow", "clay-lime-propeller", "clay-magenta-cat-ears", "clay-sand-straw-hat", "clay-powder-headlamp", "clay-cherry-sailor"
].map((f) => `${DIR}${f}.png`);
// djb2 over the name's code points: the same name always gets the same picture
export const hash = (t) => { let h = 5381; for (const ch of t) h = ((h << 5) + h + ch.codePointAt(0)) >>> 0; return h; };

// The "Hub" row has a picture of its own, in a file of its own (a/hub.png): a replacement is that file, nothing else changes. It is
// drawn for a hub with no icon of its own; today the file is a copy of one of the 24 heads
export const HUB_FACE = DIR + "hub.png";

// The heads by role. faces.json says what each head is for: [{"file": "clay-mustard-headset.png", "tags": ["support", "liaison", …]}]
// (the designer's file, the first tag is the main one; the Hub's own head is in it too, it is not one of the 24). A specialist (role
// agent) with no icon of its own and no raiser's gets the head with a tag equal to a word of its name, else of its profile (the title
// before the first ":" of `about`): the whole name first (a tag may have a hyphen: "agent-dev"), then its words, in order. Of the heads
// with that tag the one that has it earlier in its list wins, then the first in PICTURES. No tag fits — the head by the name, as for
// a session that is no specialist
const ALIASES = [   // the start of a word → the tag it means; the words of a profile that are not themselves a tag
  ["developer", "dev"], ["analytic", "analyst"], ["researcher", "research"], ["design", "designer"], ["tester", "qa"], ["testing", "qa"],
  ["marketer", "marketing"], ["copywriter", "writer"], ["illustrat", "illustration"],
];
let byTag = new Map();   // tag → [{ url, at (its place in the list of tags of the picture), n (the place of the picture in PICTURES) }]
export const tagged = () => byTag.size > 0;
// the form of the file is read loosely: a list of {file, tags}, or an object file → {tags}; a name with or without .png; the Hub's own
// head and any file that is not one of the 24 are left out
export function setTags(data) {
  const rows = Array.isArray(data) ? data : data && typeof data === "object" ? Object.entries(data).map(([file, v]) => ({ file, ...v })) : [];
  const tags = new Map();
  for (const r of rows) {
    const url = `${DIR}${String(r?.file || "").replace(/\.png$/, "")}.png`, n = PICTURES.indexOf(url);
    if (n < 0 || !Array.isArray(r.tags)) continue;
    r.tags.forEach((t, at) => tags.set(String(t), [...(tags.get(String(t)) || []), { url, at, n }]));
  }
  byTag = tags;
}
// the file is read once when the page starts; onChange tells the page the heads may have changed. No file, or one that does not parse —
// the heads stay by name
export function loadTags(onChange) {
  if (typeof fetch === "undefined") return;
  fetch(new URL("faces.json", import.meta.url)).then((r) => (r.ok ? r.json() : null)).then((d) => { if (d) { setTags(d); if (byTag.size) onChange?.(); } }).catch(() => {});
}
const best = (rows) => rows.reduce((a, b) => (b.at < a.at || (b.at === a.at && b.n < a.n) ? b : a)).url;
const headOfWord = (w) => { const rows = byTag.get(w) || byTag.get(ALIASES.find(([from]) => w.startsWith(from))?.[1]); return rows ? best(rows) : ""; };
const headOfText = (text) => {
  const whole = String(text || "").toLowerCase().trim();
  for (const w of [whole, ...whole.split(/[^\p{L}\p{N}]+/u).filter(Boolean)]) { const u = headOfWord(w); if (u) return u; }
  return "";
};

// the pictures did not load (the page server of an older release does not serve them, no network to it): no picture is drawn, the
// emoji or the letter stays. One failed request is not that: it is asked again after each of `wait` ms, and only then the pictures
// are missing; while they are, it is asked every `every` ms, and when one loads they are back (onChange tells the page each time)
let missing = false;
export const loaded = () => !missing;
export function probe(onChange, wait = [2000, 6000], every = 30000) {
  if (typeof Image === "undefined") return;
  const attempt = (i) => {
    const img = new Image();
    img.onload = () => { if (missing) { missing = false; onChange(); } };
    img.onerror = () => {
      if (i >= wait.length && !missing) { missing = true; onChange(); }
      setTimeout(() => attempt(i + 1), i < wait.length ? wait[i] : every);
    };
    img.src = PICTURES[0];
  };
  attempt(0);
}

export const faceByName = (name) => (missing ? "" : PICTURES[hash(name || "") % PICTURES.length]);
// the head of a specialist by its role ("" — none fits, or the heads are missing)
export const faceByRole = (s) => (missing || s?.role !== "agent" || !byTag.size ? "" : headOfText(s.name) || headOfText(String(s.about || "").split(/[:—\n]/)[0].slice(0, 60)));
export const hubFace = () => (missing ? "" : HUB_FACE);
export const faceOf = (icon) => (!missing && icon && Object.hasOwn(FACES, bare(icon)) ? DIR + FACES[bare(icon)] : "");
// kit.avatars of the theme: every emoji in both forms, with and without U+FE0F (the core looks up the icon as the session has it)
export function kitFaces() {
  const out = {};
  if (missing) return out;
  for (const [e, f] of Object.entries(FACES)) for (const k of [e, `${e}️`]) out[k] = DIR + f;
  return out;
}
// the character of a session: that of the icon the owner chose, else — for a specialist — that of its role, else that of the icon it
// declares, else that of whoever raised it, else one by the name of the last in the chain.
// "" — the icon is an emoji with no character (the owner's own): it stays an emoji. byName: a Map of the snapshot's sessions
export function characterOf(s, byName) {
  let x = s;
  for (let i = 0; i < 5; i++) {
    if (x.kind === "hub" && !x.icon) return hubFace();   // the hub with no icon: its own head, as in the list
    if (x.own_icon) return faceOf(x.own_icon);   // the owner chose it (the picker): nothing comes before it
    const role = faceByRole(x);   // a specialist's role comes before the emoji its profile declares
    if (role) return role;
    if (x.icon) return faceOf(x.icon);
    const up = x.raised_by && byName.get(x.raised_by);
    if (!up || up === x) break;
    x = up;
  }
  return faceByRole(x) || faceByName(x.name);
}
