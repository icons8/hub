"""Plugin channel posts inside /api/stream (a stream opened with ?feed=<plugin>[,<plugin>…], feature `stream.feed`).

A client that follows the Inbox (a plugin channel) used to poll /api/feed?plugin=…&since=N. Here the stream it already
reads carries the channel's new posts as an event of their own:

    event: feed
    data: {"plugin": "inbox", "posts": [{"id": "p812", …}, …]}

The posts are the rows /api/feed gives, in order. They start after what the stream's own first snapshot holds (its
`feeds`, the last number of each channel; the history is /api/feed), so nothing falls between that snapshot and the
events. `feed_since=<N>` starts after the post number N instead: a post's number is its journal number, one count for all
channels, so a client that comes back gives the `seq` of the last snapshot it held (the stream sends a post before the
patch that brings its number). Posts never change, so the event is
only ever a new post; a client that was away longer than the channel's window (reducer.FEED_MAX) gets the window. The
snapshot and patches are the same as without the parameter.
"""
import json
import re
from urllib.parse import parse_qs, urlsplit

from core import views

MAX_PLUGINS = 8
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")   # a plugin channel's name, as the core takes a post (reducer.post_row)


def parse(path):
    """({plugin: None} in the order asked, feed_since or None) from the stream's address; a name that does not look like a
    channel name is skipped, a bad number is ignored."""
    q = parse_qs(urlsplit(path).query)
    names = {}
    for chunk in q.get("feed", ()):
        for name in chunk.split(","):
            if NAME.fullmatch(name) and len(names) < MAX_PLUGINS:
                names[name] = None
    since = (q.get("feed_since") or [""])[0]
    # ASCII digits only: "²" is a digit to isdigit() and not a number to int()
    return names, (int(since) if since.isascii() and since.isdigit() and len(since) < 16 else None)


def start(names, feeds, since):
    """{plugin: the number to read after}: the client's own, or the channel's last post now (0 for an empty one)."""
    return {n: since if since is not None else (int(feeds[n][-1]["id"][1:]) if feeds.get(n) else 0) for n in names}


def step(cursors, feeds):
    """The `feed` events (dictionaries) for posts after each cursor; the cursors move on."""
    out = []
    for name, seen in cursors.items():
        window = feeds.get(name, ())
        if not window or int(window[-1]["id"][1:]) <= seen:   # nothing new: the window is not read through on every ping
            continue
        posts = views.posts_since(window, seen)
        if posts:
            cursors[name] = int(posts[-1]["id"][1:])
            out.append({"plugin": name, "posts": posts})
    return out


def frame(event):
    return f"event: feed\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()
