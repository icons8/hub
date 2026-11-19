"""An example module (Module API, EXTENDING.md "Core modules"): functions with the decorators of the hubmod helper.

- launch.env — the HUB_MOD_HELLO_GREETING variable for every session;
- cli — the `hub hello [whom]` command;
- doctor — its own check in `hub doctor`;
- the /api/mod/hello/count handler — a counter in the module's own state (mod.hello.set);
- the /api/mod/hello/say handler — a message to a session on behalf of the owner (hubmod.owner), only on the owner's request;
- the /mod/hello/view/ view — a page with the counter: calls its own handler through hub.json (hubmod.VIEW_JS).
"""
import os
import sys

sys.path.insert(0, os.environ.get("HUB_REPO") or os.getcwd())   # the core: hub-up supplies HUB_REPO and PYTHONPATH
from core import hubmod  # noqa: E402


@hubmod.hook("launch.env")
def launch_env(req):
    return {"env": {"HUB_MOD_HELLO_GREETING": f"hello, {req['session']}"}}


@hubmod.hook("cli")
def cli(req):
    who = " ".join(req.get("args") or []) or "owner"
    return {"out": f"hello, {who}\n", "err": "", "code": 0}


@hubmod.hook("doctor")
def doctor(req):
    return [{"name": "greeting", "ok": True, "detail": f"module API {req.get('api')}"}]


@hubmod.route("/count", methods=["GET", "POST"])
def count(req):
    n = hubmod.state.get("count", 0)
    if req.method == "POST":
        n += 1
        hubmod.state.set("count", n)
    return {"count": n}


@hubmod.route("/say", methods=["POST"])
def say(req):
    # the owner's word to a session, like the page's input field: only on the owner's request (otherwise hubmod.owner is 403);
    # the client id is the repeat key: the core will not accept the same request a second time
    body = req.json()
    if not isinstance(body, dict) or not body.get("text"):
        raise hubmod.HTTPError(400, "JSON {to, text, id} is required")
    hubmod.owner(req, "message.received", None, {"to": str(body.get("to") or ""), "text": str(body.get("text") or "")},
                 dedup_key=str(body["id"]) if body.get("id") else None)
    return hubmod.Response({"ok": True}, status=202)


PAGE = """<!doctype html><meta charset="utf-8"><body style="font: 16px system-ui">
<p>Hello! Clicked: <b id="n">…</b></p><button id="b">more</button>"""
SCRIPT = """<script>
const show = (r) => { document.getElementById("n").textContent = r.count; };
hub.json("/count").then(show);
document.getElementById("b").onclick = () => hub.json("/count", { method: "POST", json: {} }).then(show);
</script>"""


@hubmod.view("/")
def page(req):
    return PAGE + hubmod.VIEW_JS + SCRIPT


if __name__ == "__main__":
    hubmod.run()
