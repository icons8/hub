// A <details> the owner opened stays open when its view is rebuilt: Settings and the schedule are drawn anew with every
// core snapshot, and a fresh <details> starts closed. Call it right after creating the element; the key names the block
// in its view ("hubs:more"), `open` is the state before the owner has touched it.
const state = new Map();
export function keepOpen(d, key, open = false) {
  d.open = state.has(key) ? state.get(key) : open;
  d.addEventListener("toggle", () => state.set(key, d.open));
}
