// A patch of /api/stream?delta=2 (modules/web/stream_delta.py): whole sections as before, plus `_delta` — the changed
// items of a big section — and `_n`, the patch number of this connection. Returns the next snapshot (a new object; a
// changed section is a new object, the untouched sections and items are the same ones — the page compares by identity),
// or null when the patch does not fit what the page holds (a lost frame): the caller reconnects and gets a snapshot.
// A patch of a server without deltas (no `_delta`, no `_n`) is applied as it always was.
export function applyPatch(snap, patch) {
  const { _n: n, _delta: deltas, _gone: gone, ...whole } = patch;
  if (n === undefined ? deltas : n !== (snap._n || 0) + 1) return null;   // a delta always comes numbered
  const next = { ...snap, ...whole };
  for (const k of gone || []) delete next[k];
  // not enumerable: plugins get the snapshot (hub.snapshot()) with its sections only, as before; so the spread above
  // does not carry it, and a patch without a number keeps the one held
  const held = n === undefined ? snap._n : n;
  if (held !== undefined) Object.defineProperty(next, "_n", { value: held, writable: true, configurable: true });
  for (const [name, d] of Object.entries(deltas || {})) {
    const cur = next[name], list = d.by !== undefined;
    if (!cur || typeof cur !== "object" || Array.isArray(cur) !== list) return null;
    // a Map keeps the order: an existing key stays in its place, a new one goes to the end
    const rows = new Map(list ? cur.map((r) => [r[d.by], r]) : Object.entries(cur));
    for (const k of d.del || []) rows.delete(k);
    for (const [k, v] of list ? (d.set || []).map((r) => [r[d.by], r]) : Object.entries(d.set || {})) rows.set(k, v);
    let keys = [...rows.keys()];
    if (d.order) {
      if (d.order.length !== rows.size || new Set(d.order).size !== rows.size || !d.order.every((k) => rows.has(k))) return null;
      keys = d.order;
    }
    next[name] = list ? keys.map((k) => rows.get(k)) : Object.fromEntries(keys.map((k) => [k, rows.get(k)]));
  }
  return next;
}
