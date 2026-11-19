"""A trial page of the "Plugins" tab with an invented catalog — to see how it looks when there are many plugins.
A temporary hub folder, no network needed: the marketplace is replaced.

    python3 tools/dev/plugins_demo.py [port]      # default 8799; the address is http://127.0.0.1:<port>/#/settings
"""
import json
import os
import sys
import tempfile
import threading
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)

from core import journal  # noqa: E402

COLORS = ["#4f46e5", "#0891b2", "#16a34a", "#ca8a04", "#dc2626", "#9333ea", "#db2777", "#0d9488", "#ea580c", "#475569"]


def svg(letter, color):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
            f'fill="{color}"/><text x="32" y="42" font-size="30" text-anchor="middle" fill="#fff" '
            f'font-family="-apple-system,sans-serif" font-weight="600">{letter}</text></svg>')


CATALOG = [
    ("teammate", "flagged", "mascot", "An owl at the foot of the sidebar shows the state of the fleet: asleep, working, waiting for you",
     "See whether the fleet needs you without reading the session list", ["web"], True),
    ("acme", "hub-plugin-stats", "stats", "A summary of the day: how many turns, tasks and questions, who worked how much",
     "Understand what the fleet's day went on", ["web", "run"], True),
    ("acme", "hub-plugin-themes", "themes-pack", "Six page themes: \"Dusk\", \"Paper\", \"Terminal\" and others", "",
     ["themes"], True),
    ("acme", "hub-plugin-jira-board", "jira-board", "A tab with the Jira boards of your projects right in the hub",
     "No switching between the hub and Jira when agents take tasks", ["web", "run"], True),
    ("teammate", "hub-plugin-standup", "standup", "Every morning collects what the agents did yesterday and sends it to Telegram",
     "A morning summary without reading the feeds", ["run", "skills"], True),
    ("bob", "hub-plugin-cost", "cost-guard", "", "", ["run"], False),
    ("bob", "hub-plugin-voice-local", "voice-local", "Local speech recognition: whisper.cpp and the large-v3 model",
     "Voice without the cloud", ["run"], True),
    ("carol", "hub-plugin-figma", "figma-sync", "Figma skills and MCP for frontend agents: take a mockup, compare it with the layout",
     "Agents see the design, not a description of it", ["skills"], False),
    ("carol", "hub-plugin-notion", "notion-notes", "The result of every task as a Notion page", "", ["run"], True),
    ("dave", "hub-plugin-pomodoro", "pomodoro", "A pomodoro timer in the page header, \"I'm away\" when it runs out",
     "Do not forget the breaks", ["web"], True),
    ("dave", "hub-plugin-old-api", "old-widget", "A widget for an earlier version of the hub", "Was handy in 0.x", ["web"], True),
    ("erin", "hub-plugin-sentry", "sentry-feed",
     "New Sentry errors of your projects as a \"waiting for you\" card and a task for the agent in charge of that project",
     "Production errors do not get lost in the mail, they find their agent at once", ["run", "skills"], True),
]


REVIEWS = {
    "standup": {"state": "checking", "commit": "9f3c2a1"},
    "voice-local": {"state": "ok", "commit": "4be771d", "summary": "Runs whisper.cpp locally; the network only for the model download on the first start."},
    "notion-notes": {"state": "risky", "commit": "c0ffee1", "summary": "Sends task results to Notion whole, with paths and file names — worth knowing."},
}


def main(port):
    state = tempfile.mkdtemp(prefix="plugins-demo-")
    os.environ["HUB_STATE_DIR"] = state
    cfg_path = os.path.join(state, "hub.json")
    os.environ["HUB_CONFIG"] = cfg_path
    exts = {}
    for i, (acc, repo, name, desc, why, kinds, icon) in enumerate(CATALOG[:3]):
        path = os.path.join(state, "market", acc, repo)
        os.makedirs(path)
        man = {"name": name, "description": desc, "why": why, "version": "1.2.0", "hub": "^1.0"}
        if icon:
            man["icon"] = "icon.svg"
            with open(os.path.join(path, "icon.svg"), "w") as f:
                f.write(svg(name[0].upper(), COLORS[i % len(COLORS)]))
        with open(os.path.join(path, "hub-plugin.json"), "w") as f:
            json.dump(man, f, ensure_ascii=False)
        exts[f"{acc}:{name}"] = {"path": path, "source": f"{acc}/{repo}", "commit": "a1b2c3d4", "version": "1.2.0",
                                 "hub": "^1.0", **({"web": "main.js"} if "web" in kinds else {}),
                                 **({"disabled": True} if i == 2 else {})}
    with open(cfg_path, "w") as f:
        json.dump({"extensions": exts}, f, ensure_ascii=False)
    j = journal.Journal(os.path.join(state, "journal.jsonl"))
    j.append("session.requested", "hub-core", {"cwd": "/w", "session_id": "s-h", "kind": "hub"})
    j.append("session.started", "hub-core", {"session_id": "s-h"})

    from modules.market import market as mk
    catalog = []
    for i, (acc, repo, name, desc, why, kinds, icon) in enumerate(CATALOG):
        p = {"name": name, "repo": f"{acc}/{repo}", "description": desc, "why": why, "version": "1.0.0",
             "hub": "^0.1" if name == "old-widget" else "^1.0", "web": "main.js" if "web" in kinds else None,
             "run": ["python3", "main.py"] if "run" in kinds else None, "themes": ["t.css"] if "themes" in kinds else [],
             "skills": "skills" in kinds, "link": True,
             "icon_url": "data:image/svg+xml;utf8," + urllib.parse.quote(svg(name[0].upper(), COLORS[i % len(COLORS)]))
             if icon else None,
             "missing": [k for k, v in (("icon", icon), ("description", desc), ("why", why)) if not v],
             **({"review": REVIEWS[name]} if name in REVIEWS else {})}
        catalog.append(p)

    def search(self, query=""):
        from core import version
        out = []
        for p in catalog:
            st, why = version.compat(p.get("hub"))
            out.append({**p, "kinds": mk.kinds(p), "installed": False, "compat": st, "warning": why})
        return out
    mk.Market.search = search

    from modules.web.server import Web
    web = Web(state)
    srv = web.serve(port=port)
    threading.Thread(target=web.tail.run, args=(web.stop,), daemon=True).start()
    print(f"demo page: http://127.0.0.1:{port}/#/settings  (folder {state})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8799)
