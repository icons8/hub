// Clean's characters: pictures of sessions (AI-generated, see THIRD-PARTY-NOTICES.md), png files in a/. A session whose icon is one of
// these emoji shows its character instead of the emoji everywhere the page draws the icon (js/themes.js puts them into kit.avatars);
// a session with no icon gets one of them by its name, always the same (clean.js); an emoji of the owner's own stays an emoji
export const DIR = "/static/clean/a/";
// an emoji with and without U+FE0F is one icon on the screen (groups.js bare): the keys here are the bare form, one emoji to a picture
// (the icon picker has one cell per picture: a second emoji would be a cell that is never the chosen one)
export const bare = (e) => e.replace(/️/g, "");
export const FACES = {
  "🤖": "clay-terracotta-stack.png", "🧭": "clay-teal-donut.png", "🧑‍💻": "fuzzy-cobalt-fringe.png",
  "🔬": "clay-ivory-periscope.png", "🎨": "fuzzy-lime-star.png", "🧪": "clay-olive-asterisk.png",
  "🧩": "fuzzy-lavender-wide.png", "🖌": "clay-mint-skeptic.png", "✍": "fuzzy-ochre-curl.png",
  "⚙": "clay-pink-double.png", "🛠": "fuzzy-rust-moustache.png", "💬": "clay-lavender-arch.png",
  "📊": "fuzzy-cream-brows.png", "🔎": "fuzzy-teal-ring.png", "🚀": "clay-apricot-crescent.png",
  "⚡": "clay-butter-zigzag.png", "🧊": "clay-sky-cube.png", "🌙": "clay-navy-pill.png", "🌊": "clay-raspberry-wave.png",
  "🎉": "fuzzy-pink-tuft.png", "🐛": "fuzzy-mint-caterpillar.png", "🎧": "fuzzy-charcoal-bangs.png",
  "🔮": "fuzzy-plum-cone.png", "🐜": "fuzzy-cocoa-pompom.png"
};
// the pictures in a fixed order: a name takes the one at hash % 24, so the order is the contract, not the order of FACES above
export const PICTURES = [
  "clay-terracotta-stack", "clay-teal-donut", "fuzzy-cobalt-fringe", "clay-ivory-periscope", "fuzzy-lime-star", "clay-olive-asterisk",
  "fuzzy-lavender-wide", "clay-mint-skeptic", "fuzzy-ochre-curl", "clay-pink-double", "fuzzy-rust-moustache", "clay-lavender-arch",
  "fuzzy-cream-brows", "fuzzy-teal-ring", "clay-apricot-crescent", "clay-butter-zigzag", "clay-sky-cube", "clay-navy-pill",
  "clay-raspberry-wave", "fuzzy-pink-tuft", "fuzzy-mint-caterpillar", "fuzzy-charcoal-bangs", "fuzzy-plum-cone", "fuzzy-cocoa-pompom"
].map((f) => `${DIR}${f}.png`);
// djb2 over the name's code points: the same name always gets the same picture
export const hash = (t) => { let h = 5381; for (const ch of t) h = ((h << 5) + h + ch.codePointAt(0)) >>> 0; return h; };

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
export const faceOf = (icon) => (!missing && icon && Object.hasOwn(FACES, bare(icon)) ? DIR + FACES[bare(icon)] : "");
// kit.avatars of the theme: every emoji in both forms, with and without U+FE0F (the core looks up the icon as the session has it)
export function kitFaces() {
  const out = {};
  if (missing) return out;
  for (const [e, f] of Object.entries(FACES)) for (const k of [e, `${e}️`]) out[k] = DIR + f;
  return out;
}
// the character of a session: that of its icon, else that of whoever raised it, else one by the name of the last in the chain.
// "" — the icon is an emoji with no character (the owner's own): it stays an emoji. byName: a Map of the snapshot's sessions
export function characterOf(s, byName) {
  let x = s;
  for (let i = 0; i < 5; i++) {
    if (x.icon) return faceOf(x.icon);
    const up = x.raised_by && byName.get(x.raised_by);
    if (!up || up === x) break;
    x = up;
  }
  return faceByName(x.name);
}
