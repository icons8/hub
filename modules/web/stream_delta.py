"""Item deltas inside a section of /api/stream (the stream opened with ?delta=2, feature `stream.delta2`).

A plain patch resends every top-level section whose value changed: one session that started working resends the whole
`sessions` list, one new message the whole `messages` dictionary — hundreds of KB per event on a busy hub. Here a
changed section that is a dictionary, or a list of objects with a unique string `id` or `name`, goes as its changed
items only:

    "_delta": {"sessions": {"by": "name", "set": [{…the whole item…}], "del": ["gone"], "order": ["a", "b", …]},
               "messages": {"set": {"m7": {…}}, "del": ["m1"]}}

The client drops `del`, puts each `set` item in place of the one with the same key (a new key goes to the end) and, when
`order` is there, lines the items up by it (it names every key). Everything else in the patch is as it was: a section
that has no keys, is small, or changed almost whole comes whole. `_n` counts the patches of one connection from 1: a
client that sees another number than the next one has lost a frame and reconnects — the stream starts with a snapshot.

Works on the JSON the stream has already built and sent (immutable, exactly what the client holds); a stream without
?delta=2 does not come here. Nothing is kept per connection: the split of a section is shared by all streams.
"""
import json

MIN = 2048            # a smaller section goes whole: nothing to save
KEYS = ("id", "name")  # a list is keyed by the first of these that every item has as a unique string
# section -> (its JSON, split(JSON)) of the newest JSON seen, and section -> (old, new, delta(old, new)): every delta
# stream asks for the same step, so the section is parsed once per change, not once per stream, and what is kept is one
# split per big section, not one per stream or per version. A stream that lags asks for another step: its old side is
# split again, uncached. Plain dictionaries: a race between two streams only repeats the work
_splits, _deltas = {}, {}


def _dumps(v):
    return json.dumps(v, ensure_ascii=False)


def split(text):
    """(key field, {key: the item's JSON} in order) of a section's JSON; the key field is None for a dictionary and ""
    for an empty list (it fits any field); None — the section has no keys."""
    try:
        value = json.loads(text)
    except ValueError:
        return None
    if isinstance(value, dict):
        return None, {k: _dumps(v) for k, v in value.items()}
    if not isinstance(value, list):
        return None
    if not value:
        return "", {}
    if not all(isinstance(item, dict) for item in value):
        return None
    for by in KEYS:
        keys = [item.get(by) for item in value]
        if all(isinstance(k, str) for k in keys) and len(set(keys)) == len(keys):
            return by, {k: _dumps(item) for k, item in zip(keys, value)}
    return None


def _split(name, text, keep):
    hit = _splits.get(name)
    if hit and hit[0] == text:
        return hit[1]
    out = split(text)
    if keep:
        _splits[name] = (text, out)
    return out


def delta(name, old, new):
    """The JSON of the delta of section name from its JSON old to new, or None — send the section whole."""
    hit = _deltas.get(name)
    if hit and hit[0] == old and hit[1] == new:
        return hit[2]
    a = _split(name, old, False)   # the old side first: it is the kept split unless this stream lags
    out = _delta(a, _split(name, new, True), new)
    _deltas[name] = (old, new, out)
    return out


def _delta(a, b, new):
    if a is None or b is None or (a[0] is None) != (b[0] is None):
        return None
    by = a[0] or b[0]
    if by is not None and (not by or (a[0] and b[0] and a[0] != b[0])):
        return None
    was, now = a[1], b[1]
    changed = [k for k, item in now.items() if was.get(k) != item]
    gone = [k for k in was if k not in now]
    parts = []
    if by is not None:
        parts.append(f'"by":{_dumps(by)}')
    if changed:
        parts.append('"set":[' + ",".join(now[k] for k in changed) + "]" if by is not None
                     else '"set":{' + ",".join(f"{_dumps(k)}:{now[k]}" for k in changed) + "}")
    if gone:
        parts.append(f'"del":{_dumps(gone)}')
    # where the client's rules leave the items: the kept ones as they stood, the new ones after them
    if [k for k in was if k in now] + [k for k in changed if k not in was] != list(now):
        parts.append(f'"order":{_dumps(list(now))}')
    out = "{" + ",".join(parts) + "}"
    return out if len(out) < len(new) else None


def patch(last, parts, n=None):
    """The data of a `patch` event from the sent {section: JSON} last to parts, or None when nothing changed; n — the
    frame number of this connection."""
    whole, deltas = [], []
    for k, v in parts.items():
        old = last.get(k)
        if old == v:
            continue
        d = delta(k, old, v) if old is not None and max(len(old), len(v)) >= MIN else None
        (whole if d is None else deltas).append(f"{json.dumps(k)}:{v if d is None else d}")
    if deltas:
        whole.append('"_delta":{' + ",".join(deltas) + "}")
    gone = [k for k in last if k not in parts]
    if gone:
        whole.append(f'"_gone":{json.dumps(gone)}')
    if not whole:
        return None
    if n is not None:
        whole.append(f'"_n":{n}')
    return "{" + ",".join(whole) + "}"
