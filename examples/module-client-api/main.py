"""An example module for a client with a different API (Module API 2.4, EXTENDING.md "A module for a client with a different API"): a client
written for another hub uses its own paths and reads its own snapshot keys — the module answers it there.

- the /api/notes handler (http.paths): GET — the list of notes, POST {text} — add one; the module receives it as
  /api/mod/client-api/api/notes, so the code has a plain @hubmod.route("/api/notes");
- the /api/notes-view view (http.views): HTML with the list, without X-Hub, in the core sandbox;
- the notes key (snapshot.keys): the same as mods.client-api.notes, but in the snapshot root — while the module is ready and has written it
  in this run (so after a start the module writes it again — @hubmod.on_ready); the notes feature
  (snapshot.features) — in the snapshot's features, while the module is ready.
"""
import html
import os
import sys
import threading

sys.path.insert(0, os.environ.get("HUB_REPO") or os.getcwd())   # the core: hub-up supplies HUB_REPO and PYTHONPATH
from core import hubmod  # noqa: E402

MAX = 20
LOCK = threading.Lock()   # the handlers run in server threads, on_ready in its own: read and write in one step


@hubmod.route("/api/notes", methods=["GET", "POST"])
def notes(req):
    if req.method == "GET":
        return {"notes": hubmod.state.get("notes", [])}
    body = req.json()
    if not isinstance(body, dict) or not str(body.get("text") or "").strip():
        raise hubmod.HTTPError(400, "JSON {text} is required")
    with LOCK:
        items = (hubmod.state.get("notes", []) + [str(body["text"])[:200]])[-MAX:]
        hubmod.state.set("notes", items)   # and in the snapshot under the key notes
    return {"notes": items}


@hubmod.on_ready
def publish():
    # the client does not see the snapshot key of the previous run: notes are kept in the journal — show them again
    with LOCK:
        items = hubmod.state.get("notes")
        if items is not None:
            hubmod.state.set("notes", items)


@hubmod.view("/api/notes-view")
def page(req):
    rows = "".join(f"<li>{html.escape(x)}</li>" for x in hubmod.state.get("notes", []))
    return f'<!doctype html><meta charset="utf-8"><body style="font: 16px system-ui"><ul>{rows}</ul>'


if __name__ == "__main__":
    hubmod.run()
