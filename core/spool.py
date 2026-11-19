"""Mailbox of incoming facts: hooks and CLI put a file, hubd picks it up and writes it to the journal.

The write is atomic (temp file + rename), the order is by name (time in nanoseconds + pid).
"""
import json
import os
import re
import threading
import time
import uuid

from core.forms import outside_id
from core.modules import NAME as _MODULE

_last = 0
_lock = threading.Lock()
_SURROGATE = re.compile("[\ud800-\udfff]")


def _stamp():
    """Time for the name, strictly growing within the process: the Windows clock moves in millisecond steps, and two
    facts in a row got the same time — the random tail of the name decided the order (08.10)."""
    global _last
    with _lock:
        _last = max(time.time_ns(), _last + 1)
        return _last


def put(spool_dir, type, entity=None, data=None, src="cli", dedup=None):
    """dedup — the repeat key of a module fact (hubmod.fact): a file field, hubd writes it as an event field, not into data."""
    os.makedirs(spool_dir, exist_ok=True)
    name = f"{_stamp():020d}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    item = {"type": type, "entity": entity, "data": data or {}, "src": src, "ts": time.time(),
            **({} if dedup is None else {"dedup": dedup})}
    text = json.dumps(item, ensure_ascii=False)
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        # a lone surrogate (a byte that was not UTF-8 in argv, stdin or a hook's JSON) cannot be written as UTF-8 and
        # the fact was lost with a traceback: it becomes U+FFFD, the words around it stay
        raw = _SURROGATE.sub("\ufffd", text).encode("utf-8")
    tmp = os.path.join(spool_dir, "." + name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(spool_dir, name + ".json"))
    return name


def pending(spool_dir):
    """How many facts await the core: only finished .json — an unfinished .tmp (put is still writing or died) does not count."""
    try:
        return sum(n.endswith(".json") for n in os.listdir(spool_dir))
    except FileNotFoundError:
        return 0


RESTART_GAP = 15   # s: hubd was just stopped or is just starting — the mailbox is not yet its business
SLOW_START = 60    # s: hubd's pulse is fresh, but it has not taken the oldest fact for so long
PULSE_FRESH = 30   # s: hubd touches its pulse file every tick (core/daemon.py ALIVE; a tick with git can take long)
PULSE = "hubd.alive"   # the same file name as daemon.ALIVE (the daemon imports this module): the status only reads its time


def stalled(spool_dir, state_dir, now=None):
    """Why waiting facts deserve a warning in `hub status`: None — nothing (the mailbox is empty, or its oldest fact is young: a
    restart takes seconds), "down" — hubd's pulse is stale (not running), "slow" — the pulse is fresh, but hubd has not taken
    the oldest fact for SLOW_START seconds. The age is in the file name (time in nanoseconds). Reads the pulse file only: no
    lock of the daemon's (a probe holding hubd.lock could make a starting hubd think another one runs)."""
    try:
        names = [n for n in os.listdir(spool_dir) if n.endswith(".json")]
    except OSError:   # no mailbox: nothing waits
        return None
    now = now or time.time()
    age = 0
    for n in names:   # a file not named by us counts as old: it is the warning of before
        try:
            age = max(age, now - int(n.split("-")[0]) / 1e9)
        except ValueError:
            age = float("inf")
    if not names or age < RESTART_GAP:
        return None
    try:
        fresh = now - os.path.getmtime(os.path.join(state_dir, PULSE)) < PULSE_FRESH
    except OSError:
        fresh = False
    if not fresh:
        return "down"
    return "slow" if age >= SLOW_START else None


def malformed(item):
    """Why a mailbox file cannot be a fact (None — it can): a JSON object, data an object or null. One that is not used
    to crash hubd on every start (item.get on a list). A type or entity of the wrong shape is still a fact: the journal
    writes it and the fold rejects it with a reason (the CLI and the module learn of the refusal)."""
    if not isinstance(item, dict):
        return "not a JSON object"
    if item.get("data") is not None and not isinstance(item["data"], dict):
        return "data is not an object"
    return None


OUTSIDER_SRC = ("module", "plugin")   # hubmod (src module), former plugin processes (plugin), ext:<name>, mod:<name>


def _named(text, prefix=""):
    if isinstance(text, str) and text.startswith(prefix) and _MODULE.fullmatch(text[len(prefix):]):
        return text[len(prefix):]
    return None


def outsider(item):
    """A fact from a core module or a plugin process -> (data, refusal); not their fact -> (None, None). The mailbox is a
    shared file, and the channel and sender grant rights (guards: session/hub — hub and session, no channel — core,
    web/cli/telegram — the owner): for a module and a plugin the core writes these itself — channel plugin, owner false
    and the sender (session names have no ":", so another name cannot be claimed). Their fact is one with a module field,
    with src module, plugin, ext:<name>, mod:<name> or channel plugin, mod:<name>. The owner's word from a module handler
    (hubmod.owner: src module, channel mod:<name>, owner true) stays, without sender. The sender is the first that
    applies: src ext:<name> → ext:<name>; a module named by the module field, src mod:<name> or sender mod:<name>
    (hubmod.fact, bin/hub from a module) → mod:<name>; a bare name from bin/hub in a plugin → ext:<name>; own state
    mod.<name>.* from hubmod → mod:<name>. None of these — a refusal (sender mod:?); mod.<another>.* — a refusal;
    module.error — only about the sending module itself (data.module; never from a plugin process). A boundary for the
    API and for errors, not protection from code of the same user: he can forge src and a mailbox file anyway."""
    src, kind = item.get("src"), item.get("type")
    data = item.get("data") or {}
    ch, sender = data.get("channel"), data.get("sender")
    ext = src[4:] if isinstance(src, str) and src.startswith("ext:") and len(src) > 4 else None
    if not ("module" in item or src in OUTSIDER_SRC or ext or _named(src, "mod:") or ch == "plugin"
            or (isinstance(ch, str) and ch.startswith("mod:"))):
        return None, None
    if src == "module" and _named(ch, "mod:") and data.get("owner") is True:
        return {k: v for k, v in data.items() if k != "sender"}, None
    own = kind.split(".")[1] if isinstance(kind, str) and kind.startswith("mod.") and kind.count(".") >= 2 else None
    name = _named(item.get("module")) or _named(src, "mod:") or _named(sender, "mod:")
    if ext:
        who = f"ext:{ext}"
    elif name:
        who = f"mod:{name}"
    elif src != "module" and isinstance(sender, str) and sender:
        who = outside_id(sender)
    elif src == "module" and _named(own):
        who = f"mod:{own}"
    else:
        who = None
    data = {**data, "channel": "plugin", "owner": False, "sender": who or "mod:?"}
    if not who:
        return data, "a module fact without a name: the sender is mod:<name> (hubmod.fact sets it itself)"
    if own is not None and who != f"mod:{own}":
        return data, f"{kind}: the state of module {own} is written only by the module itself"
    if kind == "module.error" and who != f"mod:{data.get('module')}":
        return data, "module.error from a module is only about itself: failures of another module add up to its stop"
    return data, None


def set_aside(path, why):
    """A file that cannot be a fact goes to .bad next to it (visible for analysis, the queue moves on)."""
    print(f"hubd: mailbox file {os.path.basename(path)} set aside: {why}", flush=True)
    try:
        os.replace(path, path + ".bad")
    except OSError:
        pass


HELD = 10   # seconds: the OS has refused to open a file for this long without a break - it is not a moment's hold, set it aside
_held = {}  # path -> when (monotonic) the OS first refused to open it, in a row


def take(spool_dir):
    """[(path, event)] in order. The caller deletes the file — after writing to the journal."""
    try:
        names = sorted(n for n in os.listdir(spool_dir) if n.endswith(".json"))
    except FileNotFoundError:
        return []
    for gone in [p for p in list(_held) if os.path.dirname(p) == spool_dir and os.path.basename(p) not in names]:
        _held.pop(gone, None)   # taken by another, or set aside
    out = []
    for n in names:
        path = os.path.join(spool_dir, n)
        try:
            with open(path, encoding="utf-8") as f:
                out.append((path, json.load(f)))
        except PermissionError:
            # Windows: a fact just renamed in is held for a moment (a scanner): it is good; the rest wait with it (order)
            if time.monotonic() - _held.setdefault(path, time.monotonic()) < HELD:
                break
            set_aside(path, f"the OS has not let it be opened for {HELD} s")
            if not os.path.exists(path):
                del _held[path]   # still there (the rename was refused too): the next pass tries again, no new wait
        except (OSError, ValueError):
            try:
                os.replace(path, path + ".bad")  # a broken file must not block the queue
            except OSError:
                pass   # Windows: the file is held — the next pass will try again
        else:
            _held.pop(path, None)
    return out
