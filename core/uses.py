"""Which skills and MCP tools a session really used (spec d2547143): from the journal, for the page's "Skills and MCP"
(/api/session/kit). The owner was sure the sessions used all their skills, and they did not.

What counts as a use is read from the facts the hosts already write: a `Skill` step (`turn.tool`), a command `/<skill>`
that opened a turn (`turn.started`: the owner's or a session's `hub slash`), a read of a skill's SKILL.md (`Read`, or a
shell `cat`/`sed`… of it — that is how Codex reads a skill) and an MCP step `mcp__<server>__<tool>`. Which skill a name
or a path is, is decided by the session's set (core/kit.py: `session.kit`) of the same conversation, not by the set at
the time the page asks — a plugin swapped later does not rewrite the past. Before a session had a set (an older hub)
only `Skill`, MCP and commands that are not built-in are counted; reads of a file are not (no paths).

The journal is read like core/usage.py: only the lines with a needed type mark. Each archive is digested once, by a
separate process (core/history.py does the same: the memory peak goes away with the process), into a file next to it
(<archive>.uses.json): raw keys summed by (session, conversation, day), the sets, the conversations' starts. A copy of a
session tail that rotation carried into the next journal is dropped by its number (the archive's name ends with its
last event's number); a fact the core rejected is dropped; a call is matched with its result by id, across archives
too. The page asks a session at a time: the digests are interpreted then, in time order.

Next to the calls the digest keeps how many turns each conversation had on each day (`turns`): the denominator of "opened
in 6 conversations of 351". A digest made before that has no `turns` and is made again (_load) — its other keys are
the same, so a hub still running the earlier code reads the new one as it did."""
import json
import os
import re
import subprocess
import sys
import threading
import time

import platforms
from core import backbone, journal, kit, mcpwatch, usage

FORMAT = 2   # 2: who held a name (removal, rename), early unmatched results, keys without tabs
SIDE = ".uses.json"
TYPES = tuple(f'"type":"{t}"'.encode() for t in ("turn.tool", "turn.tool_result", "turn.started", "session.started",
                                                  "session.kit", "session.info", "session.remove_requested",
                                                  "session.renamed", "core.rejected"))
DAY = 86400
# a call open when the journal rotated gets its result in the next one, soon: unmatched results of that first stretch are
# kept for the reader (rotation also carries the call itself; compaction of archives does not keep that copy). 17.11:
# 10 minutes keep ~10 KB of results per archive, an hour ~90 KB
CARRY_WINDOW = 600
RAW_MAX = 2000    # a raw name or path, as kept (a set's paths are not cut: a longer one would never match)
ARGS = ("name", "skill", "id", "slug", "ref", "topic")   # an MCP call's argument that names what it opened, in this order
IDENT = re.compile(r"[\w.:@/-]{1,64}")
READERS = {"cat", "sed", "less", "nl", "awk"}   # a shell command that shows a file to the model
SKILL_MD = re.compile(r"""[^\s'"`;&|<>()]*/SKILL\.md\b""")
SEGMENT = re.compile(r"&&|\|\||[;|\n]")
SHELLS = re.compile(r"""^\s*(?:\S*/)?(?:ba|z)?sh\s+-\w*c\s+(['"])(.*)\1\s*$""", re.S)
ARCHIVE_NAME = re.compile(r"journal-\d+-(\d+)\.jsonl$")
TIMEOUT = 3600    # the digest of every archive by the clock: then it is killed and tried again no earlier than RETRY
RETRY = 600
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PERIODS = {"7": 7, "30": 30, "all": 0}   # /api/session/kit?days=: 0 — all the time
STATE_KEYS = ("state", "was", "raised_by", "kit", "info", "session_id")   # what report reads of a session


# --- one fact -> a raw record -----------------------------------------------------------------------------------
def _arg(inp):
    """What an MCP call opened: the first of ARGS with an identifier value, else the only identifier among the
    top-level strings; not by key order (a call with the same arguments in another order is the same call)."""
    if not isinstance(inp, dict):
        return None
    for key in ARGS:
        if isinstance(inp.get(key), str) and IDENT.fullmatch(inp[key]):
            return f"{key}={inp[key]}"
    found = [(k, v) for k, v in inp.items() if isinstance(v, str) and IDENT.fullmatch(v)]
    return f"{found[0][0]}={found[0][1]}" if len(found) == 1 else None


def read_paths(command):
    """The SKILL.md paths a shell command shows to the model: its parts (`&&`, `;`, `|`) whose first word reads a file;
    `bash -lc '…'` is unwrapped (that is how Codex runs a command). `echo …/SKILL.md` is not a read."""
    text = str(command or "")
    for _ in range(3):
        wrapped = SHELLS.match(text)
        if not wrapped:
            break
        text = wrapped.group(2)
    out = []
    for part in SEGMENT.split(text):
        words = part.split()
        if words and os.path.basename(words[0]) in READERS:
            out += [p.strip("'\"") for p in SKILL_MD.findall(part)]
    return list(dict.fromkeys(out))


def _clean(text):
    """A raw name, path or argument as a part of a key: no tabs (the key's separator), bounded."""
    return str(text).replace("\t", " ")[:RAW_MAX]


def _raw(e):
    """The records of one journal fact: [dict] (most facts give none)."""
    t, d, s = e.get("type"), e.get("data") or {}, e.get("entity")
    base = {"seq": e["seq"], "ts": e["ts"], "s": s}
    if t == "core.rejected":
        return [{"k": "rej", "seq": e["seq"], "of": d.get("of")}] if isinstance(d.get("of"), int) else []
    if not isinstance(s, str) or not isinstance(d, dict):
        return []
    if t == "turn.tool":
        name, inp = str(d.get("name") or ""), d.get("input") if isinstance(d.get("input"), dict) else {}
        call = {**base, "k": "call", "id": d.get("id"), "p": bool(d.get("parent"))}
        if name == "Skill" and isinstance(inp.get("skill"), str):
            return [{**call, "raw": "tool:" + _clean(inp["skill"])}]
        if name.startswith("mcp__") and name.count("__") >= 2:
            _, server, tool = name.split("__", 2)
            arg = _arg(inp)
            return [{**call, "raw": _clean(f"mcp:{server}/{tool}"), "arg": _clean(arg) if arg else None}]
        if name == "Read" and str(inp.get("file_path") or "").endswith("/SKILL.md"):
            return [{**call, "raw": "file:" + _clean(inp["file_path"])}]
        if name == "Bash" and "SKILL.md" in str(inp.get("command") or ""):
            return [{**call, "raw": "file:" + _clean(p)} for p in read_paths(inp.get("command"))]
        return []
    if t == "turn.tool_result":
        return [{"k": "res", "seq": e["seq"], "ts": e["ts"], "id": d.get("id"), "ok": not d.get("error"),
                 "text": bool(str(d.get("text") or "").strip())}]
    if t == "turn.started":
        cmd = backbone.command(d.get("prompt_head"))
        turn = {**base, "k": "turn"}
        return [turn, {**base, "k": "call", "id": None, "p": False, "raw": "cmd:" + _clean(cmd)}] if cmd else [turn]
    if t == "session.started" and isinstance(d.get("session_id"), str):
        return [{**base, "k": "conv", "conv": d["session_id"]}]
    if t == "session.remove_requested":
        return [{**base, "k": "gone"}]   # the name is free: a later session of it is another one
    if t == "session.renamed":
        return [{**base, "k": "gone"}]   # the same: its uses go on under the new name (core/reducer: sess["was"])
    if t == "session.kit" and isinstance(d.get("skills"), list):
        return [{**base, "k": "kit", "skills": [[r.get("name"), r.get("path")] for r in d["skills"]
                                                 if isinstance(r, dict) and isinstance(r.get("name"), str)]}]
    if t == "session.info" and isinstance(d.get("builtins"), list):
        return [{**base, "k": "bi", "builtins": [b for b in d["builtins"] if isinstance(b, str)]}]
    return []


def _scan(f, out):
    """The needed lines of file f from its position (the chunked scan of core/usage.py): out(record). Returns how many
    bytes were read as whole lines."""
    return usage._scan(f, lambda r: [out(x) for x in r], TYPES, _raw)


def _good(raw, ok, text):
    """A finished call counts as a use: a read of SKILL.md only when it showed something."""
    return ok and (text or not raw.startswith("file:"))


# --- a journal's records -> its digest ----------------------------------------------------------------------------
def digest(records, floor):
    """The digest of one journal's records: events up to floor (copies of the previous journal's tail) and the
    rejected ones dropped, calls matched with their results, raw keys summed by (session, conversation, day, key, arg).
    A conversation not started in this journal is "": the reader takes it from the journal before."""
    rejected = {r["of"] for r in records if r["k"] == "rej"}
    # a call of the previous journal still open there may come here again in the carried tail
    carried = {r["id"] for r in records if r["k"] == "call" and r["seq"] <= floor and r.get("id")}
    rows = sorted((r for r in records if r["k"] != "rej" and r["seq"] > floor and r["seq"] not in rejected),
                  key=lambda r: r["seq"])
    start = min((r["ts"] for r in rows if "ts" in r), default=None)
    conv, calls, sums, kits, builtins, open_, results, gone = {}, {}, {}, [], set(), [], {}, {}
    turns = {}

    def add(c, outcome):
        if c["raw"].startswith("file:") and outcome != "ok":
            return   # a read that failed or showed nothing is not a use
        key = "\t".join((c["s"], c["conv"] or "", str(int(c["ts"] // DAY)), c["raw"], c.get("arg") or ""))
        row = sums.setdefault(key, [0, 0, 0, 0, 0.0])
        row[{"ok": 0, "failed": 1, "unknown": 2}[outcome]] += 1
        if outcome == "ok" and not c["p"]:
            row[3] += 1   # the session itself (not its helper agent) used it: "in this conversation"
        row[4] = max(row[4], c["ts"])

    for r in rows:
        k = r["k"]
        if k == "conv":
            conv[r["s"]] = r["conv"]
        elif k == "gone":
            gone[r["s"]] = r["ts"]
        elif k == "turn":
            key = "\t".join((r["s"], conv.get(r["s"]) or "", str(int(r["ts"] // DAY))))
            turns[key] = turns.get(key, 0) + 1
        elif k == "kit":
            kits.append([r["s"], conv.get(r["s"]), r["ts"], r["skills"]])
        elif k == "bi":
            builtins.update(r["builtins"])   # Claude Code's own commands: the same for every session of a version
        elif k == "call":
            c = {**r, "conv": conv.get(r["s"])}   # the call's conversation, kept with it until its result
            if r.get("id"):
                calls.setdefault(r["id"], []).append(c)
            else:
                add(c, "ok")   # a command opened a turn: nothing to wait for
        elif k == "res":
            got = calls.pop(r.get("id"), None)
            if got is None:
                if r.get("id") and (r["id"] in carried or (start is not None and r["ts"] <= start + CARRY_WINDOW)):
                    results[str(r["id"])] = [r["ok"], r["text"]]   # its call is in the journal before
                continue
            for c in got:
                add(c, "ok" if _good(c["raw"], r["ok"], r["text"]) else "failed")
    for got in calls.values():
        for c in got:   # no result in this journal: the next one may have it
            open_.append([c["s"], c["conv"], c["ts"], c["raw"], c.get("arg"), c["p"], c["id"]])
    return {"format": FORMAT, "sums": sums, "turns": turns, "kits": kits, "builtins": sorted(builtins), "open": open_,
            "results": results, "conv_end": conv, "gone": gone, "from": start,
            "to": max((r["ts"] for r in rows if "ts" in r), default=None)}


def floors(paths):
    """{path: the number of the last event of the archive before it (by name), 0 for the first}; None — the live
    journal's: after every archive."""
    nums = sorted((int(m.group(1)), p) for p in paths if (m := ARCHIVE_NAME.search(os.path.basename(p))))
    out, prev = {}, 0
    for n, p in nums:
        out[p], prev = prev, n
    out[None] = prev
    return out


def _write(path, got):
    tmp = f"{path}{SIDE}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(got, f, ensure_ascii=False, separators=(",", ":"))
    platforms.current.replace(tmp, path + SIDE)
    return got


def build(path, floor):
    """Digest an archive into the file next to it (by the page's background process, main)."""
    records = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        _scan(f, records.append)
    return _write(path, {**digest(records, floor), "size": size, "floor": floor})


def _load(path, floor):
    try:
        size = os.path.getsize(path)
        with open(path + SIDE, encoding="utf-8") as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(got, dict) or got.get("format") != FORMAT or got.get("size") != size or got.get("floor") != floor \
            or "turns" not in got:   # made before the turns were kept: again (the format stays: an older hub reads the rest)
        return None
    return got


def main(state_dir):
    """A separate process digests every archive without a digest, newest first (Reader._build): it yields to the hub,
    and its memory goes away with it. 17.11 on a hub with 503 archives (44 GB): 38 s, 3.6 MB of digests, 53 MB peak.
    An archive that cannot be read gets an empty digest with the reason: an archive does not change, so it is not read
    again and again."""
    paths = journal.archives(state_dir)
    below = floors(paths)
    later = False
    for path in reversed(paths):
        floor = below.get(path, 0)
        if _load(path, floor) is not None:
            continue
        try:
            build(path, floor)
        except FileNotFoundError:
            continue   # removed meanwhile
        except (OSError, MemoryError) as e:   # the disk or the machine now: again after RETRY (exit code 3)
            print(f"uses: archive {os.path.basename(path)} not digested now: {e!r}", file=sys.stderr, flush=True)
            later = True
        except Exception as e:   # noqa: BLE001 — what the archive holds: one must not hold up the others
            print(f"uses: archive {os.path.basename(path)} not digested: {e!r}", file=sys.stderr, flush=True)
            try:
                _write(path, {**digest([], floor), "size": os.path.getsize(path), "floor": floor,
                              "error": repr(e)[:300]})
            except OSError:
                later = True
    if later:
        sys.exit(3)


# --- the page's reader --------------------------------------------------------------------------------------------
class Reader:
    """The digests of all journals of a hub for the page: the archives by one background process (newest first, so the
    last week is there first; its digests are picked up as they appear), the live journal read on from where it stopped."""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.lock = threading.Lock()
        self.digests = {}      # archive path -> its digest
        self.building = None
        self.failed = 0.0      # when the background process last failed (not again before RETRY)
        self.live = {"ino": None, "pos": 0, "records": [], "floor": None}
        self.live_lock = threading.Lock()   # the page asks from several threads: one reads the live journal on at a time

    def _archives(self):
        return journal.archives(self.state_dir)

    def _start(self):
        with self.lock:
            if self.building and self.building.is_alive() or time.time() - self.failed < RETRY:
                return
            self.building = threading.Thread(target=self._build, daemon=True, name="uses-archives")
            self.building.start()

    def _build(self):
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in (ROOT, os.environ.get("PYTHONPATH")) if p))
        code = f"import platforms; platforms.current.give_way(); from core import uses; uses.main({self.state_dir!r})"
        try:
            r = subprocess.run(platforms.current.below_hub([sys.executable, "-c", code]), cwd=ROOT, env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                               timeout=TIMEOUT, **platforms.current.background())
            why = r.returncode and (r.stderr.decode("utf-8", "replace").strip()[-300:] or f"exit code {r.returncode}")
        except (subprocess.TimeoutExpired, OSError) as e:
            why = repr(e)
        if why:   # failed: the next ask does not start it again before RETRY; done well — a new archive is read at once
            with self.lock:
                self.failed = time.time()
            print(f"uses: digesting the archives failed: {why}", file=sys.stderr, flush=True)

    def build_here(self):
        """Every archive in this process (tests, a small hub)."""
        paths = self._archives()
        below = floors(paths)
        for path in paths:
            floor = below.get(path, 0)
            got = _load(path, floor) or build(path, floor)
            with self.lock:
                self.digests[path] = got

    def _live(self, paths):
        path = os.path.join(self.state_dir, "journal.jsonl")
        floor = floors(paths)[None]
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return digest([], floor)
        c = self.live
        if c["ino"] != st.st_ino or st.st_size < c["pos"] or c["floor"] != floor:   # rotated
            c.update(ino=st.st_ino, pos=0, records=[], floor=floor)
        if st.st_size > c["pos"]:
            with open(path, "rb") as f:
                f.seek(c["pos"])
                c["pos"] += _scan(f, c["records"].append)
        return digest(c["records"], floor)

    def digests_now(self, background=True):
        """[digest] of every journal in time order, whether every archive is digested, the oldest time digested. A
        digest whose archive went away or was rewritten (journal scrub, compaction: its file next to it is deleted, the
        size changes) is forgotten."""
        paths = self._archives()
        below = floors(paths)
        with self.lock:
            cached = dict(self.digests)
        for p, d in cached.items():
            try:
                same = os.path.getsize(p) == d.get("size") and os.path.exists(p + SIDE)
            except OSError:
                same = False
            if not same or p not in below:
                with self.lock:
                    self.digests.pop(p, None)
        for p in paths:
            if p not in self.digests:
                got = _load(p, below.get(p, 0))   # the background process may have digested it meanwhile
                if got is not None:
                    with self.lock:
                        self.digests[p] = got
        with self.lock:
            have = [self.digests[p] for p in paths if p in self.digests]
        complete = len(have) == len(paths)
        if not complete and background:
            self._start()
        with self.live_lock:
            live = self._live(paths)
        older = [d.get("from") for d in have if d.get("from")]
        return have + [live], complete, min(older, default=live.get("from"))


# --- interpretation -----------------------------------------------------------------------------------------------
def _match(name, names):
    """A skill name as the set names it: exact, else the only one with the tail `:<name>`, else None."""
    if name in names:
        return name
    tails = [n for n in names if n.rpartition(":")[2] == name and ":" in n]
    return tails[0] if len(tails) == 1 else None


def collect(digests, names, turns=None):
    """The uses of sessions from digests in time order. names — {a name in the journal: the session it is now}; a
    name of its own is the session's current name, another is a former one. A name's holder changes when a session is
    removed or renamed away from it: under the current name only what came after its last such change counts, under a
    former name only what came before it — a later session of that name is another one. Returns
    [{s, conv, day, key, how, arg, ok, failed, unknown, main, last, unverified?}] and the sets {session: [(ts, conv,
    {name: path})]}; a raw key is read by the set of the same session and conversation. turns, a list, is filled with
    the turns of the same sessions: [{s, conv, day, n}] (see volume)."""
    gone = {}
    for d in digests:
        for s, ts in (d.get("gone") or {}).items():
            if s in names:
                gone.setdefault(s, []).append(ts)

    def mine(s, ts=None, day=None):
        """Whether a fact of name s at ts (or on a day) is of the session the name maps to."""
        cuts = gone.get(s) or []
        if not cuts:
            return True
        if names[s] == s:   # the current name: after its last change of holder
            return (ts if ts is not None else (day + 1) * DAY) > max(cuts)
        return (ts if ts is not None else day * DAY) <= max(cuts)   # a former name: before it left

    conv, kits, rows, pending, builtins = {}, {}, [], {}, set()
    for d in digests:
        builtins.update(d.get("builtins") or [])
        for s, c, ts, skills in d.get("kits") or []:
            if s in names and mine(s, ts):
                kits.setdefault(names[s], []).append((ts, c if c is not None else conv.get(names[s]), dict(skills)))
        for rid, (ok, text) in (d.get("results") or {}).items():   # results of calls left open by earlier journals
            for c in pending.pop(rid, []):
                good = _good(c["raw"], ok, text)
                if good or not c["raw"].startswith("file:"):
                    rows.append({**c, "ok": int(good), "failed": int(not good), "unknown": 0,
                                 "main": int(good and not c["p"])})
        for key, n in (d.get("turns") or {}).items():
            parts = key.split("\t")
            if turns is not None and len(parts) == 3 and parts[0] in names and mine(parts[0], day=int(parts[2])):
                turns.append({"s": names[parts[0]], "conv": parts[1] or conv.get(names[parts[0]]), "day": int(parts[2]),
                              "n": n})
        for key, (ok, failed, unknown, main, last) in (d.get("sums") or {}).items():
            parts = key.split("\t")
            if len(parts) != 5 or parts[0] not in names:
                continue
            s, c, day, raw, arg = parts
            if mine(s, day=int(day)):
                rows.append({"s": s, "conv": c or conv.get(names[s]), "day": int(day), "raw": raw, "arg": arg or None,
                             "p": False, "ok": ok, "failed": failed, "unknown": unknown, "main": main, "last": last})
        for s, c, ts, raw, arg, p, rid in d.get("open") or []:
            if s in names and mine(s, ts):
                pending.setdefault(str(rid), []).append({"s": s, "conv": c if c is not None else conv.get(names[s]),
                                                         "day": int(ts // DAY), "raw": raw, "arg": arg, "p": p,
                                                         "last": ts})
        for s, c in (d.get("conv_end") or {}).items():
            if s in names and c and mine(s, d.get("to")):
                conv[names[s]] = c   # by the session, not the name: a rename keeps the conversation
    for left in pending.values():   # never answered: counted, outcome unknown; a read of a file not
        rows += [{**c, "ok": 0, "failed": 0, "unknown": 1, "main": 0} for c in left if not c["raw"].startswith("file:")]
    out = []
    for r in rows:
        session = names[r["s"]]
        key = _interpret(r, kits.get(session) or [], builtins)
        if key:
            out.append({**r, "s": session, **key})
    return out, kits


def _kit_for(row, kits):
    """The set a use is read by: the one of the same conversation (its first turn too: the set comes with the first init,
    before any step), else the last before the use's day ends; none — the use is from before the session had a set."""
    same = [k for k in kits if row["conv"] and k[1] == row["conv"]]
    if same:
        names = {}
        for k in same:
            names.update(k[2])
        return names
    before = [k for k in kits if k[0] < (row["day"] + 1) * DAY]
    return before[-1][2] if before else None


def _interpret(row, kits, builtins):
    raw = row["raw"]
    kind, _, rest = raw.partition(":")
    if kind == "mcp":
        return {"key": "mcp:" + rest, "how": "tool"}
    names = _kit_for(row, kits)
    if kind in ("tool", "cmd"):
        rest = kit.name_of(rest)   # a skill of the parent repo, seen from a working copy: `.claude/worktrees/<copy>:<name>`
    if kind == "tool":
        got = _match(rest, names) if names else None
        return {"key": "skill:" + (got or rest), "how": "tool"}
    if kind == "cmd":
        if names is not None:
            got = _match(rest, names)
            return {"key": "skill:" + got, "how": "command"} if got else None
        if rest in builtins or rest in backbone.BUILTINS or rest in backbone.ENDS_SESSION:
            return None
        return {"key": "skill:" + rest, "how": "command", "unverified": True}
    if kind == "file" and names:
        by_path = {p: n for n, p in names.items() if p}
        if rest in by_path:
            return {"key": "skill:" + by_path[rest], "how": "file"}
    return None


def summary(rows, since, conversation):
    """{key: {tool, command, file, failed, unknown, convs, last, this_conversation, args, unverified}} — counts within
    the period (since), last over all the rows; convs — in how many conversations it was used."""
    out, convs = {}, {}
    for r in rows:
        e = out.setdefault(r["key"], {"tool": 0, "command": 0, "file": 0, "failed": 0, "unknown": 0, "last": None})
        e["last"] = max(e["last"] or 0, r["last"]) or None
        if conversation and r["conv"] == conversation and r["main"]:
            e["this_conversation"] = True
        if r.get("unverified"):
            e["unverified"] = True
        if (r["day"] + 1) * DAY <= since:
            continue
        used = r["ok"] + r["failed"] + r["unknown"] if r["how"] != "file" else r["ok"]
        e[r["how"]] += used
        if used:
            convs.setdefault(r["key"], set()).add((r.get("s"), r["conv"] or ""))
        e["failed"] += r["failed"]
        e["unknown"] += r["unknown"]
        if r.get("arg"):
            args = e.setdefault("args", {})
            args[r["arg"]] = args.get(r["arg"], 0) + r["ok"] + r["failed"] + r["unknown"]
    for key, e in out.items():
        e["convs"] = len(convs.get(key) or ())
        if len(e.get("args") or {}) > 20:   # a busy tool: its 20 most used arguments
            top = sorted(e["args"].items(), key=lambda kv: -kv[1])
            e["args"] = dict(top[:20])
            e["args_more"] = len(top) - 20
    return out


def volume(turns, since):
    """How much happened in the period (since), the denominator of a count of uses: {conversations, turns} of the
    turns [{s, conv, day, n}] that collect gave. A turn of a session before it had a conversation id counts as one
    conversation of its own."""
    within = [r for r in turns if (r["day"] + 1) * DAY > since]
    return {"conversations": len({(r["s"], r["conv"] or "") for r in within}), "turns": sum(r["n"] for r in within)}


def names_of(st, name):
    """A session's names: the current one and the former ones (core/reducer: sess["was"])."""
    sess = (st.get("sessions") or {}).get(name) or {}
    was = sess.get("was") or []
    return [name, *[w for w in (was if isinstance(was, list) else [was]) if isinstance(w, str) and w != name]]


def executors_of(st, name):
    return sorted(n for n, s in (st.get("sessions") or {}).items() if s.get("raised_by") == name and n != name)


def report(reader, st, name, days, now, background=True):
    """The answer of /api/session/kit: the set, the live MCP statuses, the uses of the session and of its executors."""
    sess = st["sessions"][name]
    digests, complete, read_from = reader.digests_now(background)
    since = now - days * DAY if days else 0
    own_turns, ex_turns = [], []
    own, kits = collect(digests, {n: name for n in names_of(st, name)}, own_turns)
    execs = executors_of(st, name)
    ex = collect(digests, {n: e for e in execs for n in names_of(st, e)}, ex_turns)[0] if execs else []
    first_kit = min((k[0] for k in kits.get(name) or []), default=None)
    oldest = None if complete else read_from
    lead = _lead(st, sess)
    status = mcp_status(sess)
    # a tool name carries the server's name the way Claude Code mangles it: back to the name the set uses
    servers = {mcpwatch._tool_name(m): m for m in [*status, *[r.get("name") for r in (sess.get("kit") or {}).get("mcp") or []]]
               if isinstance(m, str)}
    return {"session": name, "days": days, "complete": complete,
            "period_ready": complete or (oldest is not None and oldest <= since),
            "read_from": read_from, "files_from": first_kit, "kit": sess.get("kit"),
            "mcp_status": status, **({"lead": lead} if lead else {}),
            "used": {"own": _servers(summary(own, since, sess.get("session_id")), servers),
                     "executors": _servers(summary(ex, since, None), servers), "executors_count": len(execs)},
            "volume": {"own": volume(own_turns, since), "executors": volume(ex_turns, since)}}


def fleet(reader, st, days, now, background=True):
    """The answer of `hub session kit --fleet`: what report gives for one session, for every session of the hub at
    once — per skill and per MCP server, in how many sessions the engine loaded it and how much it was used (by any
    session, a gone one too: its uses stay in the journal). st is the state of the sessions (STATE_KEYS)."""
    digests, complete, read_from = reader.digests_now(background)
    since = now - days * DAY if days else 0
    sessions = st.get("sessions") or {}
    names = {n: n for n in sessions}
    for n in sessions:
        for w in names_of(st, n)[1:]:
            names.setdefault(w, n)   # a former name, unless a session has it now
    turns = []
    rows, _ = collect(digests, names, turns)
    by = {}
    for r in rows:
        by.setdefault(r["s"], []).append(r)
    held = {n: s for n, s in sessions.items() if isinstance(s.get("kit"), dict) and s.get("state") != "removed"}
    servers = {mcpwatch._tool_name(m["name"]): m["name"] for s in held.values()
               for m in s["kit"].get("mcp") or [] if isinstance(m, dict) and isinstance(m.get("name"), str)}
    skills, mcp, listed = {}, {}, {"known": 0, "tokens": 0}
    for s in held.values():
        for r in s["kit"].get("skills") or []:
            e = skills.setdefault(r["name"], _blank(source=None, plugin=None, tokens=None))
            e["source"], e["plugin"] = e["source"] or r.get("source"), e["plugin"] or r.get("plugin")
            e["loaded"] += 1
            e["in_prompt"] += r.get("in_prompt") is True
            e["tokens"] = max(e["tokens"] or 0, r.get("tokens") or 0) or None
        for m in s["kit"].get("mcp") or []:
            e = mcp.setdefault(m["name"], _blank(tools=0))
            e["loaded"] += 1
            e["tools"] = max(e["tools"], m.get("tools") or 0)
        tokens = (s["kit"].get("listed") or {}).get("tokens")
        if isinstance(tokens, int):
            listed["known"] += 1
            listed["tokens"] += tokens
    for rs in by.values():   # one session at a time: the counts of conversations are exact (a conversation is one session's)
        mine = {}
        for key, u in _servers(summary(rs, since, None), servers).items():
            kind, _, rest = key.partition(":")
            e = mine.setdefault((kind, rest.partition("/")[0] if kind == "mcp" else rest),
                                {"tool": 0, "command": 0, "file": 0, "failed": 0, "convs": 0, "last": 0})
            for k in ("tool", "command", "file", "failed"):
                e[k] += u[k]
            e["convs"] += u["convs"] if kind == "skill" else 0   # the tools of a server share conversations
            e["last"] = max(e["last"], u["last"] or 0)
        for (kind, name), u in mine.items():
            e = mcp.setdefault(name, _blank(tools=0)) if kind == "mcp" else skills.setdefault(
                name, _blank(source=None, plugin=None, tokens=None))
            for k in ("tool", "command", "file", "failed", "convs"):
                e[k] += u[k]
            e["used_by"] += bool(u["tool"] + u["command"] + u["file"])
            e["last"] = max(e["last"] or 0, u["last"]) or None
    for e in (*skills.values(), *mcp.values()):
        e["calls"] = e["tool"] + e["command"] + e["file"]
    turned = {r["s"] for r in turns if (r["day"] + 1) * DAY > since}
    order = lambda item: (-item[1]["calls"], item[0])   # noqa: E731 — the most used first
    return {"days": days, "complete": complete, "period_ready": complete or (read_from is not None and read_from <= since),
            "read_from": read_from, "sessions": {"with_set": len(held), "active": len(turned)},
            "volume": volume(turns, since), "listed": listed,
            "skills": [{"name": n, **e} for n, e in sorted(skills.items(), key=order)],
            "mcp": [{"name": n, **e} for n, e in sorted(mcp.items(), key=order)]}


def _blank(**more):
    return {"loaded": 0, "in_prompt": 0, "tool": 0, "command": 0, "file": 0, "failed": 0, "used_by": 0, "convs": 0,
            "last": None, **more}


def _servers(used, servers):
    out = {}
    for key, e in used.items():
        if key.startswith("mcp:"):
            server, _, tool = key[4:].partition("/")
            key = f"mcp:{servers.get(server, server)}/{tool}"
        out[key] = e
    return out


def _lead(st, sess):
    """An executor's skills its lead has and it does not (a working copy of a repo without the lead's plugins)."""
    lead = (st.get("sessions") or {}).get(sess.get("raised_by") or "")
    if not lead or not lead.get("kit") or not sess.get("kit"):
        return None
    mine = {r.get("name") for r in sess["kit"].get("skills") or []}
    missing = sorted(r["name"] for r in lead["kit"].get("skills") or []
                     if r.get("name") not in mine and r.get("source") != "built-in")
    return {"name": sess["raised_by"], "missing": missing} if missing else None


def mcp_status(sess):
    """{server: connected | failed | needs-auth | disabled | pending} as the core holds it now (core/mcpwatch)."""
    info = sess.get("info") or {}
    out = {}
    for m in info.get("mcp") or []:
        if not isinstance(m, str):
            continue
        out[m] = ("needs-auth" if m in (info.get("mcp_auth") or []) else "failed" if m in (info.get("mcp_failed") or [])
                  else "disabled" if m in (info.get("mcp_disabled") or [])
                  else "connected" if m in (info.get("mcp_connected") or []) or "mcp_connected" not in info
                  else "pending")
    return out
