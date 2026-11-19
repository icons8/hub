#!/usr/bin/env python3
"""A fake tailscale CLI for the tailnet module tests: status/serve status --json come from a file, commands go to a
log, and serve --http=P <target> and reset change the "config"."""
import json
import os
import sys

state_path = os.environ["FAKE_TS_STATE"]
state = json.load(open(state_path))
args = sys.argv[1:]
if not os.environ.get("SHLVL"):  # like the CLI from Tailscale.app: without SHLVL it assumes it was opened as a GUI
    print("The Tailscale GUI failed to start")
    sys.exit(0)
with open(os.environ["FAKE_TS_LOG"], "a") as f:
    f.write(json.dumps(args) + "\n")
if args[:2] == ["status", "--json"]:
    print(json.dumps(state["status"]))
elif args[:3] == ["serve", "status", "--json"]:
    print(json.dumps(state["serve"]))
elif args[:2] == ["serve", "reset"]:
    state["serve"] = {}
elif args[:1] == ["serve"]:
    port = next(a.split("=", 1)[1] for a in args if a.startswith("--http="))
    target = args[-1]
    host = state["status"]["Self"]["DNSName"].rstrip(".")
    state["serve"].setdefault("TCP", {})[port] = {"HTTP": True}
    state["serve"].setdefault("Web", {})[f"{host}:{port}"] = {"Handlers": {"/": {"Proxy": target}}}
json.dump(state, open(state_path, "w"))
