"""A lab hub from a copy of the live hub: a new core version is checked on a real journal, the live hub is not touched.

    python3 tools/lab_copy.py <live state folder> <live hub.json> <lab folder> <lab settings folder>

What it does with the copy (the copy itself lives in the lab folder; live files are only read):
- session ids are new: a lab session does not continue a live one's conversation (`--resume` of a foreign id would spoil
  its history); the new ids have no conversations on disk — sessions start anew (has_history is cleared);
- all sessions except the hub sleep: the lab does nothing on its own until someone writes to it;
- schedule jobs are paused (otherwise the live jobs would run twice), the message queue is cleared;
- settings: no Telegram (one token — one process), no updates, no plugins (breaks of the "Feed" would wake sessions),
  own ports; every browser is local, with its own port and profile in the lab folder; hub-dev and agent-dev are
  copies of the live ones (system, core/system.py).
The prepared state is put as a snapshot at the end of the journal copy and into state.json — both launch paths see the
same thing.
"""
import json
import os
import shutil
import sys
import urllib.parse
import uuid

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
from core import journal  # noqa: E402
from modules.browser import browsers  # noqa: E402

COPY = ("agents", "directions", "comments")   # what else is carried over together with the journal
LAB_PORTS = {"web": 8795, "files": 8796, "browser": 9225}


def remap_ids(text, ids):
    for old, new in ids.items():
        text = text.replace(old, new)
    return text


def prepare(state, now, ids, src_state=None, lab_state=None):
    """The lab state: sessions asleep (except the hub), conversations from scratch, the schedule paused, the queue cleared;
    session folders inside the live hub (hub-home, sandbox, workspaces) are the same in the lab."""
    live_root = os.path.realpath(src_state) + os.sep if src_state else None
    for s in state["sessions"].values():
        cwd = os.path.realpath(s.get("cwd") or "")
        if live_root and (cwd + os.sep).startswith(live_root):
            s["cwd"] = os.path.join(lab_state, os.path.relpath(cwd, live_root))
        s["has_history"] = False
        for k in ("pid", "pane", "socket"):   # the fields stay (commands read them), there is no process
            s[k] = None
        if s["state"] in ("removed", "closed", "failed"):
            continue
        s["pending"], s["closing"], s["background"] = [], "none", {}
        s.pop("asks", None)
        # a parked and a retired executor stay as they are (core/machines.SLEEP), the rest sleep
        s["state"] = "idle" if s.get("kind") == "hub" else s["state"] if s["state"] in ("parked", "retired") else "asleep"
        s["since"] = now
    for m in state["messages"].values():
        if m["state"] in ("queued", "typing"):
            m["state"], m["reason"] = "failed", "lab: the live hub's queue is not carried over"
    for j in (state.get("schedule") or {}).values():
        if j.get("state") == "active":
            j["state"], j["paused_why"] = "paused", "lab: live jobs do not run twice"
    return state


def lab_ports(cfg):
    """Ports of the lab browsers: consecutive from LAB_PORTS["browser"], skipping the live browsers' ports (their address — as the core
    sees it, browsers.configured: the default port, the port as a string) — otherwise the lab Chrome will not start, and the lab
    sessions would reach the live Chrome with the owner's logins over CDP."""
    taken = set()
    for name, b in (cfg.get("browsers") or {}).items():
        try:
            taken.add(urllib.parse.urlsplit(str(browsers.configured({"browsers": {name: b}})[name].get("url") or "")).port)
        except (ValueError, TypeError, AttributeError):   # an entry the core cannot start: the port is not a number, etc.
            pass
    port = LAB_PORTS["browser"]
    while True:
        if port not in taken:
            yield port
        port += 1


def lab_browser(b, name, port, lab_state):
    """The lab browser: always local, with its own port and an empty profile in the lab folder — no login of the live one
    is available. The address and headers of a remote one (the owner's Chrome over CDP) are not carried over, nor are the profile and port flags."""
    keep = {k: v for k, v in b.items() if k not in ("url", "headers")}
    args = [a for a in keep.get("args") or [] if not str(a).startswith(("--user-data-dir", "--remote-debugging"))]
    if "args" in keep:
        keep["args"] = args
    return {**keep, "kind": "local", "port": port, "risk": [],
            "profile": os.path.join(lab_state, "browsers", name),
            "description": "the lab's Chrome — its own profile, without the working hub's logins"}


def lab_config(src_cfg, lab_state):
    cfg = json.load(open(src_cfg, encoding="utf-8"))
    cfg["hub"] = {**(cfg.get("hub") or {}), "cwd": os.path.join(lab_state, "hub-home")}
    cfg["extensions"] = {}
    cfg["update"] = {"enabled": False}
    cfg["title"] = "Lab"
    ports = lab_ports(cfg)
    cfg["browsers"] = {name: lab_browser(b, name, next(ports), lab_state)
                       for name, b in sorted((cfg.get("browsers") or {}).items())}
    cfg["tailnet"] = {"expose": {"web": LAB_PORTS["web"], "files": LAB_PORTS["files"]}}
    return cfg


def main(src_state, src_cfg, lab_state, lab_cfg_dir, now=None):
    import time
    now = now or time.time()
    if os.path.exists(os.path.join(lab_state, "journal.jsonl")):
        sys.exit(f"{lab_state}: the lab already exists — delete the folder to build it again")
    os.makedirs(lab_state, exist_ok=True)
    os.makedirs(lab_cfg_dir, exist_ok=True)
    live = journal.load_state(src_state)
    ids = {s["session_id"]: str(uuid.uuid4()) for s in live["sessions"].values() if s.get("session_id")}
    with open(os.path.join(src_state, "journal.jsonl"), encoding="utf-8") as f, \
            open(os.path.join(lab_state, "journal.jsonl"), "w", encoding="utf-8") as out:
        for line in f:
            out.write(remap_ids(line, ids))
    for name in COPY:
        src = os.path.join(src_state, name)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(lab_state, name), dirs_exist_ok=True)
    for sub in ("hub-home", "sandbox", "workspaces", "logs", "bin"):
        os.makedirs(os.path.join(lab_state, sub), exist_ok=True)
    state = json.loads(remap_ids(json.dumps(live, ensure_ascii=False), ids))   # taken before the journal copy: does not run ahead of it
    state = prepare(state, now, ids, src_state, lab_state)
    j = journal.Journal(os.path.join(lab_state, "journal.jsonl"))
    ev = j.append("core.snapshot", data={"why": "lab-copy", "state": {k: v for k, v in state.items()
                                                                       if not k.startswith("_") and k != "seq"}}, ts=now)
    state["seq"] = ev["seq"]
    journal.save_state(lab_state, state)
    with open(os.path.join(lab_cfg_dir, "hub.json"), "w", encoding="utf-8") as f:
        json.dump(lab_config(src_cfg, lab_state), f, ensure_ascii=False, indent=2)
    live_sessions = [n for n, s in state["sessions"].items() if s["state"] not in ("removed", "closed", "failed")]
    print(f"lab: {lab_state}, events {ev['seq']}, sessions {len(live_sessions)} (only the hub is awake), "
          f"ids replaced {len(ids)}")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
