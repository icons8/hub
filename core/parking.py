"""A free session with nothing to do does not hold a process (owner decision 2026-10-07: do not start a session's
process until someone writes to it). It is parked: the process is not started after a hub restart, or is stopped after a
long idle time, the conversation stays; the first message brings it up with the same conversation (reducer.new_message
wakes a sleeping one). On the page a parked session shows "free", not "asleep": nothing changed for the owner."""

from core import specmode
from core.catalog import MCP_TIMEOUT
from core.machines import COMMAND_CONFIRM_SECONDS

PARK_AFTER_HOURS = 2   # free for longer — the process is stopped (hub.json: park_after_hours, 0 — do not stop)
PENDING = ("queued", "typing")
MCP_WAIT = int(MCP_TIMEOUT) / 1000   # seconds a new process may wait for its MCP servers before it reads its input


def parkable(st, sess, tasks=True, now=None, gone=False):
    """Whether the process can be released: free, not a hub, no background work, nothing undelivered, no open question,
    no task in progress (tasks=False — do not look), no deferred tool reload, no conversation compaction (scheduled or
    running: otherwise parking stops the process in the middle of /compact; running for no longer than
    COMMAND_CONFIRM_SECONDS from now — the result may not have arrived, and a hung process is exactly the one parking
    should stop). gone — the process is already gone (hub restart): compaction is no obstacle: a running one was
    interrupted, a scheduled one is no reason to start a process."""
    if sess.get("kind") == "hub" or sess["state"] != "idle":
        return False
    if not gone and (sess.get("compact") or compacting(sess, now)):
        return False
    name = sess["name"]
    if sess.get("background") or sess.get("pending") or sess.get("dirty") or sess.get("browser_stale"):
        return False
    if any(m["session"] == name and m["state"] in PENDING for m in st["messages"].values()):
        return False
    # a task in work, or one whose spec waits for the owner's "Accept" (core/specmode.py: owner decision 2026-11-08,
    # the waiting executor is kept alive)
    return not tasks or not any(d.get("session") == name and (d.get("state") == "working" or specmode.is_open(d)
                                                              and specmode.holds(d))
                                for d in (st.get("delegations") or {}).values())


def compacting(sess, now=None):
    """/compact is running: sent and no result yet, but for no longer than COMMAND_CONFIRM_SECONDS (without now — assume it is running)."""
    return bool(sess.get("compacting")) and (now is None or now - sess["compacting"] < COMMAND_CONFIRM_SECONDS)


def mcp_init(sess, now):
    """A new process has not read its input yet: Claude Code takes the first line only once its MCP servers are up
    (`system init`, session.info), and waits for them up to MCP_TIMEOUT - after a full restart the whole fleet connects
    at once. A line typed meanwhile sits in its stdin: neither a retry nor "not delivered" until then."""
    return bool(sess.get("mcp_init")) and now - sess["mcp_init"] < MCP_WAIT


def idle_too_long(sess, now, hours=PARK_AFTER_HOURS):
    """Free for longer than `hours` hours (0 or less — do not stop)."""
    return bool(hours and hours > 0) and sess["state"] == "idle" and now - (sess.get("since") or now) >= hours * 3600


COMPACT_AFTER_MINUTES = 30      # free this long — compact a long conversation (hub.json: compact_idle.after_minutes, 0 — no)
COMPACT_MIN_TOKENS = 200000     # a conversation longer than this — compact it (compact_idle.min_tokens)
CACHE_WARM_MINUTES = 55         # the conversation cache lives an hour: later, compaction would re-upload it whole — no longer worth it
LONG_TIMES, LONG_AFTER_MINUTES = 2, 5   # twice the threshold — compact after only 5 minutes of quiet (09.10: a secretary
                                        # with a round every 30 minutes was never free for half an hour — 450k and growing)


def compactable(st, sess, now, cfg=None):
    """Compact a long conversation while the cache is warm (owner decision 2026-10-08): free for after_minutes but less than
    an hour, the conversation longer than min_tokens, no compaction since the last turn, nothing to do (as for parking, but a hub too)."""
    cfg = cfg or {}
    after = cfg.get("after_minutes", COMPACT_AFTER_MINUTES)
    if not after or after <= 0 or sess["state"] != "idle" or sess.get("compact") or sess.get("idle_compacted"):
        return False
    least = cfg.get("min_tokens", COMPACT_MIN_TOKENS)
    if (sess.get("context") or 0) < least:
        return False
    if (sess.get("context") or 0) >= LONG_TIMES * least:
        after = min(after, LONG_AFTER_MINUTES)
    idle = now - (sess.get("since") or now)
    if not after * 60 <= idle < CACHE_WARM_MINUTES * 60:
        return False
    # things to do — as for parking, but a pending browser reload (it restarts with the next message) and an open
    # task do not block compaction: the process stays (09.10: a devops session with browser_stale was never compacted;
    # 15.10: an agent with a task "in progress" grew to 846k in a day and a half)
    return parkable(st, {**sess, "kind": "work", "browser_stale": False}, tasks=False, now=now)
