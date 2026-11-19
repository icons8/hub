"""Module manifest for the core catalog: the direction skill is the `directions` tool, the hub gives it to the direction's session."""
import json
import os

PLUGIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin")


def entries(cfg):
    with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    return {"directions": {"kind": "plugin", "path": PLUGIN, "description": manifest["description"], "risk": [],
                           "source": "directions"}}
