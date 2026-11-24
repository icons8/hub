#!/usr/bin/env python3
"""The schedule and the guarantee of the stops audit: the precheck of the core job `agent-dev-stops` (core/system.py).

A "stop" is a rule in an agent's CLAUDE.md that makes it ask, show or wait for the owner before acting. The script finds
no stops itself (agent files are in any language; a model reads them — agent-dev, skill agent-developer, section
"6a. Stops audit"). It only decides when an audit is due, makes sure it is not skipped and prepares cheap input for it.

  stops-audit.py [--check]     is an audit due? No model, no network, does not read the journal: seconds.
                               0 — not due; 3 — due (reasons and the next commands); 1 — an error, one line.
  stops-audit.py --bundle      prepares <state>/stops-audit/bundle/: INDEX.md (per agent: full — the whole file, first pass;
                               delta — only lines added or changed since the last audit; unchanged — skip) and
                               cards-<agent>.md — groups of same-kind owner cards of the last --days days that were
                               accepted as proposed. Reads the journal and its archives (with a cache: the first run reads
                               a week of archives, later runs only new ones) — the heavy step.
  stops-audit.py --extract     a cheap model (claude -p, no tools, stops-extract.md next to this file) lists the stops
                               of full/delta in chunks → bundle/stops.json and stops.md; what agent-dev's ledger already holds
                               (the same agent, >= 0.7 of the quote's word stems) and what waits for nobody (thresholds, watchers:
                               `waits_owner: false`) is left out and counted → skipped.json. Never called by the core.
                               A failed call (exit code, is_error, no JSON array, price 0) is not "0 stops": exit 1, the
                               agent goes to state.json "extract_failed", the next --extract takes only such agents, --done
                               refuses them (unless --skip). Runs on the pool account like the watchdog.
                               A hub on Codex (experiment): the hub's Codex model (core/codexmodel), no claude.
  stops-audit.py --done [TEXT] [--skip AGENT…]   the audit is done: remembers the files as they went into the bundle.
  stops-audit.py --status      one line: when the last audit was, whether one is requested.

An audit is due when there was none; after --every days if a file changed or agent-dev's ledger (review/stops.md in the folder of its
session) has open entries (asked, temporary); after --gap days if an agent's file grew by --grow bytes of new lines; and while a requested audit is not
marked --done: every tick repeats it ("MISSED N d"), it cannot be postponed. No agents — never due.
"Accepted as proposed": every question of the card was answered with its recommended option (no recommended — the
first); that is a reason to ask whether the question is needed, not a permission.

The state folder is found as the core finds it (core/daemon.default_state_dir): $HUB_STATE_DIR (the schedule clock and
hub sessions have it), else the path the install wrote, else the OS default.
"""
import argparse
import difflib
import functools
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

DAY = 86400
SLACK = 0.25   # days: the daily tick comes at the same hour, an audit marked minutes after it is still "a week ago" then
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(__file__)
NEW = 3   # the precheck code for "there is news" (core/schedule.CHECK_NEW)
# the heavy step on a Linux machine with a user systemd: in its own cgroup, so the rest of the machine is not touched
HEAVY = ["systemd-run", "--user", "--scope", "-p", "CPUQuota=200%", "-p", "MemoryMax=6G", "-p", "MemorySwapMax=0",
         "nice", "-n", "19"]
CHUNK = 30000   # bytes of numbered lines per model call
GONE = ("removed", "closed", "failed")   # agents that are not running (core/system.GONE)
# the ledger of agent-dev (skill agent-developer, section 6a): an open entry — a question asked and not answered, a
# temporary hold — is re-checked every week even when no file changed; it lies in the folder of agent-dev's session
LEDGER = ("review", "stops.md")
OPEN = re.compile(r"\b(asked|temporary)\b", re.I)   # in the status field: after the last "·" of an entry line


def core_on_path():
    """The core's modules (core, platforms, modules.*) importable from this script, which lies in modules/agent_dev."""
    root = os.path.dirname(os.path.dirname(HERE))
    if root not in sys.path:
        sys.path.insert(0, root)


@functools.lru_cache(maxsize=None)
def hub_dir():
    """As core/daemon.default_state_dir (not imported: the daemon module pulls the whole core into a seconds-long check)."""
    if os.environ.get("HUB_STATE_DIR"):
        return os.environ["HUB_STATE_DIR"]
    core_on_path()
    import platforms
    from core import statemark
    return statemark.default_state() or platforms.current.state_dir()


def own_dir(*parts):
    d = os.path.join(hub_dir(), "stops-audit", *parts)
    os.makedirs(d, exist_ok=True)
    return d


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read()


def write_text(path, text):
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def load_json(path, default):
    """Read whole and closed before parsing: the hub's state.json is tens of MB, and while a reader holds it, Windows
    refuses hubd the replace (core/journal.read_state_file — not imported here, see hub_dir)."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return default
    got = json.loads(text)
    return got if isinstance(got, type(default)) else default


# ---- cards of the owner from the journal (with a cache) ----------------------------------------------------------
def card_lines(path):
    """Lines of permission.* events of a journal file: grep where there is one (fast on gigabytes), else line by line.
    The pattern has the type key, so the huge core.snapshot lines never match. Not on Windows: the grep of Git for
    Windows gets the pattern's quotes spoiled (\\type":...) and finds nothing — no cards, silently."""
    needle = '"type":"permission.'
    if os.name != "nt":
        try:
            p = subprocess.run(["grep", "-h", "-F", needle, path], capture_output=True, stdin=subprocess.DEVNULL)
            if p.returncode in (0, 1):
                return p.stdout.decode("utf-8", "replace").splitlines()
        except OSError:
            pass
    with open(path, encoding="utf-8", errors="replace") as f:
        return [line for line in f if needle in line]


def slim(e):
    d = e.get("data") or {}
    if e.get("type") == "permission.requested" and d.get("tool") == "AskUserQuestion":
        qs = [{"h": str(q.get("header") or ""), "q": str(q.get("question") or ""),
               "o": [str(o.get("label") or "") for o in q.get("options") or [] if isinstance(o, dict)]}
              for q in (d.get("input") or {}).get("questions") or [] if isinstance(q, dict)]
        return {"k": "R", "seq": e["seq"], "ts": e["ts"], "e": e.get("entity"), "rid": d.get("request_id"), "qs": qs}
    if e.get("type") == "permission.answered":
        return {"k": "A", "seq": e["seq"], "ts": e["ts"], "rid": d.get("request_id"), "a": d.get("answers")}
    return None


def load_cards(days, budget, now):
    """Owner cards (AskUserQuestion) of the last `days` days with their answers. Archives already read are remembered by
    size (an archive does not change), the live journal is read every time; duplicates are dropped by seq."""
    cpath, mpath = os.path.join(own_dir(), "cards.jsonl"), os.path.join(own_dir(), "archives.json")
    seen = load_json(mpath, {})
    lo, keep_lo = now - days * DAY, now - 2 * days * DAY
    evs = {}
    if os.path.exists(cpath):
        with open(cpath, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                evs[e["seq"]] = e
    hub = hub_dir()
    archives = [f for f in glob.glob(os.path.join(hub, "journal-archive", "journal-*.jsonl")) if os.path.getmtime(f) >= lo]
    todo = sorted((f for f in archives if seen.get(os.path.basename(f)) != os.path.getsize(f)),
                  key=os.path.getmtime, reverse=True)   # the newest first: a cut budget loses the oldest
    live = os.path.join(hub, "journal.jsonl")
    t0, left = time.time(), 0
    for i, f in enumerate(todo + ([live] if os.path.exists(live) else [])):
        if f != live and time.time() - t0 > budget:
            left = left or len(todo) - i   # the rest of the archives: the next --bundle goes on from them
            continue
        size = os.path.getsize(f)
        for line in card_lines(f):
            try:
                s = slim(json.loads(line))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            if s and s["ts"] >= keep_lo:
                evs[s["seq"]] = s
        if f != live:
            seen[os.path.basename(f)] = size
    keep = sorted((e for e in evs.values() if e["ts"] >= keep_lo), key=lambda e: e["seq"])
    write_text(cpath, "".join(json.dumps(e, ensure_ascii=False, separators=(",", ":")) + "\n" for e in keep))
    names = {os.path.basename(f) for f in archives}
    write_text(mpath, json.dumps({k: v for k, v in seen.items() if k in names}))
    answers = {e["rid"]: e for e in keep if e["k"] == "A"}
    cards = []
    for e in keep:
        if e["k"] != "R" or e["ts"] < lo or not e["qs"]:
            continue
        a = answers.get(e["rid"])
        c = {"seq": e["seq"], "ts": e["ts"], "entity": e["e"], "qs": e["qs"], "answered": bool(a),
             "answers": (a or {}).get("a") if isinstance((a or {}).get("a"), dict) else {}}
        c["acc"] = accepted(c) if a else None
        cards.append(c)
    return cards, left


def _recommended():
    """The "(Recommended)" mark of an option, in English and in every language with data (core/langdata.py)."""
    core_on_path()
    from core import langdata   # stdlib only: no seconds-long import of the core
    return re.compile(r"ecommended|" + langdata.alt("words", "recommended"), re.I)


REC = _recommended()


def accepted(c):
    """Every question of the card was closed with its recommended option (no recommended one — with the first)."""
    for q in c["qs"]:
        a, labels = c["answers"].get(q["q"]), q["o"]
        if a is None or not labels:
            return False
        rec = [x for x in labels if REC.search(x)]
        if a not in (rec or labels[:1]):
            return False
    return bool(c["qs"])


def lead(c):
    q = c["qs"][0]
    t = (q["h"] + " " + q["q"]).replace("\n", " ")
    m = re.search(r"\?", t)
    return t[:m.end()] if m and m.end() < 260 else t[:200]


def stems(text):
    """Words of 4+ letters (any script), first 5 characters as a rough stem; numbers and ids are dropped."""
    return {w[:5] for w in re.findall(r"[^\W\d_]{4,}", re.sub(r"[#!]?\d+", " ", text.lower()))}


def clusters(cards, min_cards, share):
    """Groups of same-kind cards of one agent (single link, Jaccard of the first question's words >= 0.25) with at least
    min_cards answered and at least `share` of them accepted as proposed: [(agent, cards, answered, accepted, sample)]."""
    by = {}
    for c in cards:
        by.setdefault(c["entity"], []).append(c)
    out = []
    for ent, cs in by.items():
        sets = [stems(lead(c)) for c in cs]
        par = list(range(len(cs)))

        def find(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i
        for i in range(len(cs)):
            for j in range(i):
                u = sets[i] | sets[j]
                if u and len(sets[i] & sets[j]) / len(u) >= 0.25:
                    par[find(i)] = find(j)
        groups = {}
        for i, c in enumerate(cs):
            groups.setdefault(find(i), []).append(c)
        for g in groups.values():
            ans = [c for c in g if c["answered"]]
            acc = sum(1 for c in ans if c["acc"])
            if len(ans) >= min_cards and acc / len(ans) >= share:
                out.append((ent, len(g), len(ans), acc, lead(max(g, key=lambda c: c["ts"]))[:160]))
    return sorted(out, key=lambda r: (r[0], -r[1]))


# ---- agents, own state, the bundle -------------------------------------------------------------------------------
def load_agents():
    """Agents of the hub with their own CLAUDE.md: {name: {path, sha, size}}. The file is agents/<profile>/CLAUDE.md in
    the state folder; the profile is the session's `profile` or its session id (hosts/streamjson.agent_file)."""
    st = load_json(os.path.join(hub_dir(), "state.json"), {})
    if not st and os.path.exists(os.path.join(hub_dir(), "journal.jsonl")):
        # a journal rotation removes state.json until hubd saves it again: the state is the journal's
        core_on_path()
        from core import journal
        st = journal.load_state(hub_dir())
    if not st:
        raise RuntimeError(f"no hub state {os.path.join(hub_dir(), 'state.json')}")
    st = st.get("state", st)
    out = {}
    for name, s in (st.get("sessions") or {}).items():
        if not isinstance(s, dict) or s.get("role") != "agent" or s.get("state") in GONE:
            continue
        profile = s.get("profile") or s.get("session_id")
        if not profile:
            continue
        p = os.path.join(hub_dir(), "agents", str(profile), "CLAUDE.md")
        if os.path.isfile(p):
            with open(p, "rb") as f:
                body = f.read()
            out[name] = {"path": p, "sha": hashlib.sha256(body).hexdigest()[:12], "size": len(body)}
    return out


def state_path():
    return os.path.join(own_dir(), "state.json")


def delta(name, path):
    """New and changed lines of the file against the last audit's snapshot: [(line number, text)]; None — no snapshot."""
    sp = os.path.join(own_dir("snap"), name + ".md")
    if not os.path.exists(sp):
        return None
    old, new = read_text(sp).split("\n"), read_text(path).split("\n")
    out = []
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out += [(j + 1, new[j]) for j in range(j1, j2) if new[j].strip()]
    return out


def mode(name, agent, st):
    """full (first pass), delta or unchanged; with the delta lines."""
    if not st.get("audited_at") or name not in (st.get("files") or {}):
        return "full", None
    if (st.get("files") or {}).get(name) == agent["sha"]:
        return "unchanged", []
    dl = delta(name, agent["path"])
    if dl is None:
        return "full", None
    return ("delta" if dl else "unchanged"), dl


def write_bundle(agents, cards, st, args):
    d = own_dir("bundle")
    for f in glob.glob(os.path.join(d, "*")):
        if os.path.isfile(f):
            os.remove(f)
    pend = own_dir("pending")
    for f in glob.glob(os.path.join(pend, "*.md")):
        os.remove(f)
    cl = clusters([c for c in cards if c["entity"] in agents], args.min_cards, args.share)
    index, rows = {}, []
    for name, a in sorted(agents.items()):
        # what goes into the bundle is what --done remembers: edits made during the audit land in the next delta
        shutil.copyfile(a["path"], os.path.join(pend, name + ".md"))
        m, dl = mode(name, a, st)
        entry = {"mode": m, "file": a["path"], "sha": a["sha"]}
        if m == "full":
            entry["bytes"] = a["size"]
        else:
            body = "\n".join(f"L{n}: {t}" for n, t in dl)
            entry["bytes"] = len(body.encode())
            if dl:
                entry["delta"] = os.path.join(d, f"delta-{name}.md")
                write_text(entry["delta"], f"# {name}: new and changed lines of {a['path']} since the last audit\n\n{body}\n")
        mine = [r for r in cl if r[0] == name]
        if mine:
            entry["cards"] = os.path.join(d, f"cards-{name}.md")
            write_text(entry["cards"], f"# {name}: similar questions to the owner over {args.days} days, accepted as proposed\n\n"
                       + "".join(f"- {n} cards, {acc} of {ans} answered accepted as proposed · example: {sample}\n"
                                 for _, n, ans, acc, sample in mine))
        index[name] = entry
        rows.append(f"- {name} · {m} · {entry['bytes']} B · groups {len(mine)} · "
                    + {"full": a["path"], "delta": entry.get("delta", "-"), "unchanged": "-"}[m]
                    + (f" · {entry['cards']}" if mine else ""))
    write_text(os.path.join(d, "index.json"), json.dumps(index, ensure_ascii=False, indent=1))
    write_text(os.path.join(d, "INDEX.md"),
               "# Stops audit data\n\nAgent · mode (full — the whole file, first pass; delta — only what changed; "
               "unchanged — nothing) · volume to read · groups of accepted questions · files\n\n" + "\n".join(rows) + "\n")
    return d


# ---- the cheap extraction (a model; only from a session, never from the core) -------------------------------------
def chunks_of(lines, size=CHUNK):
    out, cur, n = [], [], 0
    for line in lines:
        if cur and n + len(line.encode()) > size:
            out.append("\n".join(cur))
            cur, n = [], 0
        cur.append(line)
        n += len(line.encode()) + 1
    return out + (["\n".join(cur)] if cur else [])


def codexmodel():
    core_on_path()
    from core import codexmodel as cm
    return cm


def on_codex():
    """Codex (experimental): this hub runs on Codex (hub.json "host": "codex") — there is no claude, the extraction
    goes to the hub's Codex (core/codexmodel)."""
    return codexmodel().on_codex(hub_dir())


CLAUDE_ALIASES = ("haiku", "sonnet", "opus")


def one_codex(name, chunk, model, prompt):
    """One chunk on a hub on Codex: the same prompt as the thread's instructions, the answer as text (a JSON array, as
    from claude). A Claude alias (--model haiku by default) is the core's cheap Codex model; any other name — that Codex
    model. Tokens go to the journal (codex.spent of codex:agent-dev-stops), there is no price."""
    cm = codexmodel()
    try:
        text = cm.ask(chunk, prompt, None, state_dir=hub_dir(),
                      model=None if model in CLAUDE_ALIASES else model, timeout=900, by="agent-dev-stops")
    except cm.Error as e:
        return name, [], 0, f"codex: {e}"
    m = re.search(r"\[.*\]", text or "", re.S)   # as from claude (reply): no array is a failure, not "no stops"
    if not m:
        return name, [], 0, f"no JSON array in the reply: {' '.join((text or '').split())[:80]!r}"
    try:
        found = json.loads(m.group(0))
    except ValueError:
        return name, [], 0, f"cannot parse the array in the reply: {m.group(0)[:80]!r}"
    return name, [x for x in found if isinstance(x, dict)], 0, None


KNOWN = 0.7   # the share of a quote's stems found in one ledger entry of the same agent: the line is already known
MIN_STEMS = 3   # a shorter quote shares its two words with any entry: it is never called known
BOOK_LINE = re.compile(r"^\s*-\s*(.+?)\s*·\s*«(.*)»\s*·")   # - <agent> · «<quote>» · <class> · <status>


def book_quotes(text):
    """[(agent, quote, stems)] of the ledger lines; no ledger (None) or no such lines — [], no comparison."""
    out = []
    for line in (text or "").splitlines():
        m = BOOK_LINE.match(line)
        if m:
            out.append((m.group(1), m.group(2), stems(m.group(2))))
    return out


def sift(found, book):
    """(rest, known, other): lines the ledger does not hold; how many it already held; how many wait for nobody."""
    rest, known, other = [], 0, 0
    for s in found:
        if s.get("waits_owner") is False:   # the model saw a threshold or a watcher, not a question to the owner
            other += 1
            continue
        mine = stems(str(s.get("quote")))
        if len(mine) >= MIN_STEMS and any(a == s.get("agent") and len(mine & b) / len(mine) >= KNOWN for a, _, b in book):
            known += 1
            continue
        rest.append(s)
    return rest, known, other


class Bad(Exception):
    """The model call did not give an answer: not "no stops", a failure (the chunk stays unprocessed)."""


def model_env():
    """The environment of the nested claude -p: the pool account the sessions are on, as the watchdog and the critic do
    (modules/watch/watch.py:model_env). Without it claude goes on this machine's login, which may be expired or on
    another provider: the call fails (it gave "0 stops, $0.000")."""
    core_on_path()
    from core import journal
    from modules.watch.watch import model_env as pool_env
    try:
        account = (journal.load_state(hub_dir()).get("account") or {}).get("active")
    except Exception:   # noqa: BLE001 — no readable state: this machine's login, as without a pool
        account = None
    try:
        # from a session's Bash: os.environ is the session's, with its model remap — pool_env drops it, --model answers
        return pool_env(account=account)
    except OSError as e:   # the login module gave no profile login (core/profiles.NoProfile)
        raise RuntimeError(f"no login for claude -p (account {account}): {e}") from None


def reply(r):
    """The stops found in a finished claude -p run: [dict], and its price. Anything but a real answer - Bad: an exit
    code, an is_error answer (a sign-in refusal, a limit), no JSON, no array, a price of 0 (no request went out)."""
    try:
        res = json.loads(r.stdout)
    except ValueError:
        raise Bad(f"output is not JSON, code {r.returncode}: {((r.stderr or '') + (r.stdout or '')).strip()[-120:]}") from None
    if not isinstance(res, dict):
        raise Bad(f"output is not an object, code {r.returncode}")
    text = str(res.get("result") or "")
    if r.returncode or res.get("is_error"):
        raise Bad(f"claude: code {r.returncode}, {' '.join(text.split())[:120] or 'no reply'}")
    cost = res.get("total_cost_usd")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost <= 0:
        raise Bad("price 0 with a non-empty input: no request reached the model")
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise Bad(f"no JSON array in the reply: {' '.join(text.split())[:80]!r}")
    try:
        found = json.loads(m.group(0))
    except ValueError:
        raise Bad(f"cannot parse the array in the reply: {m.group(0)[:80]!r}") from None
    return [x for x in found if isinstance(x, dict)], cost


def run_deadline(cmd, timeout=900, **kw):
    """subprocess.run with a deadline that really ends: platforms.current.run in the call's own group (Windows: a job), so
    at the deadline everything the call started stops too and the pipes it held close (subprocess.run on Windows stopped
    only claude and read the pipes its grandchild kept with no limit: the run hung for good). The time is counted as
    subprocess.run counts it (the monotonic clock: on Linux and macOS a sleeping machine does not use it up, a call is not
    cut short on waking). The calls run in worker threads, so Ctrl+C of the audit does not reach them: each ends by its
    own deadline at the latest."""
    core_on_path()
    import platforms
    return platforms.current.run(cmd, timeout, **kw, **platforms.current.apart())


def extract(model, only, run=None, env=None):
    """-> (stops, price, failed {agent: [reason]}, known, other). An agent whose chunk failed is not processed: it is not
    in stops.json, it goes to state.json "extract_failed" and the next --extract takes only such agents again; --done
    does not mark it. run, env - the model call and its environment (tests)."""
    if os.environ.get("HUB_NO_MODEL"):   # tests: never the live model
        raise RuntimeError("the model is off (HUB_NO_MODEL)")
    d = own_dir("bundle")
    index = load_json(os.path.join(d, "index.json"), {})
    if not index:
        raise RuntimeError("no data: run --bundle first")
    st = load_json(state_path(), {})
    again = sorted(set(st.get("extract_failed") or {}) & set(index)) if not only else []   # the retry of the last run
    jobs = []
    for name, e in sorted(index.items()):
        if only and name not in only or again and name not in again:
            continue
        if e["mode"] == "delta":
            lines = [x for x in read_text(e["delta"]).split("\n")[2:] if x.strip()]   # already numbered L<n>:
        elif e["mode"] == "full":
            lines = [f"L{i}: {x}" for i, x in enumerate(read_text(e["file"]).split("\n"), 1)]
        else:
            continue
        jobs += [(name, ch) for ch in chunks_of(lines)]
    codex = on_codex()
    if env is None:
        env = model_env() if jobs and not codex else {}   # nothing to ask (or Codex): no Claude login is needed
    run = run or run_deadline
    core_on_path()
    from core import realclaude   # not the session wrapper (hosts/nested): it needs HUB_NESTED_* of a session
    claude = None if codex else realclaude.find()
    text = read_text(os.path.join(HERE, "stops-extract.md"))
    prompt = None
    if not codex:   # claude takes the prompt as a file, not an argument: Windows limits a command line
        fd, prompt = tempfile.mkstemp(prefix="stops-extract-", suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)

    def one(job):
        name, chunk = job
        try:
            if codex:
                return one_codex(name, chunk, model, text)
            r = run([claude, "-p", "--model", model, "--tools", "", "--setting-sources", "",
                     "--strict-mcp-config", "--no-session-persistence", "--output-format", "json",
                     "--system-prompt-file", prompt], input=chunk, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", cwd=tempfile.gettempdir(), timeout=900, env=env)
            found, c = reply(r)
            return name, found, c, None
        except Bad as e:
            return name, [], 0, str(e)
        except subprocess.TimeoutExpired as e:   # str(e) starts with the whole command line: the reason would be cut off
            return name, [], 0, f"no answer in {e.timeout:g} s"
        except Exception as e:   # noqa: BLE001 — any failure of one chunk is that chunk's failure: ex.map would drop the whole run
            return name, [], 0, f"{type(e).__name__}: {e}"
    from concurrent.futures import ThreadPoolExecutor
    got, cost, failed = {}, 0.0, {}
    try:
        with ThreadPoolExecutor(4) as ex:   # chunks are independent
            for name, found, c, err in ex.map(one, jobs):
                cost += c
                got.setdefault(name, []).extend({"agent": name, **s} for s in found)
                if err:
                    failed.setdefault(name, []).append(err[:200])
    finally:
        if prompt:
            os.unlink(prompt)
    for name in failed:   # an agent with a failed chunk is not half-processed: all of it again next time
        got.pop(name, None)
    redone = set(got) | set(failed)
    keep = bool(only or again)   # a partial run adds to the previous result of the same bundle
    old = load_json(os.path.join(d, "stops.json"), []) if keep else []
    old_skipped = load_json(os.path.join(d, "skipped.json"), []) if keep else []
    all_found = [s for v in got.values() for s in v]
    try:
        book = read_text(ledger_path())
    except OSError:
        book = None   # no ledger yet (the first audit): nothing to compare with
    new, known, other = sift(all_found, book_quotes(book))
    out = [s for s in old if s.get("agent") not in redone] + new
    write_text(os.path.join(d, "skipped.json"), json.dumps(
        [s for s in old_skipped if s.get("agent") not in redone] + [s for s in all_found if s not in new],
        ensure_ascii=False, indent=1))
    write_text(os.path.join(d, "stops.json"), json.dumps(out, ensure_ascii=False, indent=1))
    st = load_json(state_path(), {})   # re-read: only this key changes
    # a partial run (--only) removes from the earlier failures only the agents it has just redone successfully
    pending = {n: r for n, r in (st.get("extract_failed") or {}).items() if n in index and n not in redone}
    pending.update({n: v[0] for n, v in failed.items()})
    who = "by the hub's Codex model (tokens — in Costs)" if codex else f"by the {model} model (price ${cost:.3f})"
    write_text(os.path.join(d, "stops.md"),
               f"# Stops extracted {who}; the class is a hint, read the quote yourself\n"
               f"# dropped as known to the book: {known}; not waiting for the owner: {other} (skipped.json)\n"
               + (f"# NOT PARSED agents: {len(pending)} — this list is not enough, --extract repeats them\n" if pending else "")
               + "\n"
               + "".join(f"- {s['agent']} L{s.get('line')} [{s.get('class')}{', temporary' if s.get('temporary') else ''}"
                         f"{', owner words' if s.get('owner_word') else ''}] «{str(s.get('quote'))[:110]}»\n" for s in out)
               + "".join(f"\nNOT PARSED {n}: {'; '.join(failed.get(n) or [r])}" for n, r in sorted(pending.items())))
    if pending:
        st["extract_failed"] = dict(sorted(pending.items()))
    else:
        st.pop("extract_failed", None)
    write_text(state_path(), json.dumps(st, ensure_ascii=False, indent=1))
    return len(out), cost, failed, known, other


# ---- when it is due ------------------------------------------------------------------------------------------------
def ledger_path():
    """review/stops.md in the folder of the session agent-dev, as the core knows it: the session's cwd, else its own
    folder <state>/workspaces/<name> (core/workspace.py). The skill names this same place: "in your folder"."""
    st = load_json(os.path.join(hub_dir(), "state.json"), {})
    st = st.get("state", st)
    name = st.get("trainer") or "agent-dev"   # the trainer is agent-dev (core/daemon.py), renamed or not
    sess = (st.get("sessions") or {}).get(name)
    cwd = sess.get("cwd") if isinstance(sess, dict) else None
    return os.path.join(cwd or os.path.join(hub_dir(), "workspaces", name), *LEDGER)


def ledger_open():
    """The ledger has entries to re-check (asked and unanswered, temporary holds)."""
    try:
        text = read_text(ledger_path())
    except OSError:
        return False
    return any(OPEN.search(line.rsplit("·", 1)[1]) for line in text.splitlines()
               if line.lstrip().startswith("-") and "·" in line)


def due(agents, st, a, now):
    """[reasons] the audit is due; [] — not. No agent with a file — nothing to audit, agent-dev is not woken."""
    why = []
    last = st.get("audited_at") or 0
    age = (now - last) / DAY + SLACK
    if st.get("requested_at"):
        why.append(f"MISSED {(now - st['requested_at']) / DAY:.0f} d: the audit was requested and not marked — it cannot be "
                   f"postponed, do it and mark --done")
    if not agents:
        return why
    files = st.get("files") or {}
    changed = any(files.get(n) != x["sha"] for n, x in agents.items())   # by the sha: no diff on a quiet tick
    if not last:
        why.append("no audit yet (first pass — whole files)")
    elif age >= a.every and (changed or ledger_open()):
        # a week with no edit and nothing open in the ledger: nothing new to read, the wake is not worth it
        why.append(f"the last audit was {age - SLACK:.0f} d ago (every {a.every})")
    elif age >= a.gap and changed:
        grown = {}
        for n, x in agents.items():
            m, dl = mode(n, x, st)
            grown[n] = x["size"] if m == "full" else sum(len(t.encode()) for _, t in dl or [])
        big = {n: b for n, b in grown.items() if b >= a.grow}
        if big:
            why.append("files grew since the last audit: " + ", ".join(f"{n} +{b // 1000} KB" for n, b in sorted(big.items())))
    return why


def command(*args):
    q = lambda s: f'"{s}"'   # noqa: E731 — a path may hold spaces
    return " ".join([q(sys.executable), q(SCRIPT), *args])


def heavy_prefix():
    """On Linux with a running user systemd: the heavy step in its own cgroup (CPU and memory capped)."""
    if sys.platform.startswith("linux") and shutil.which("systemd-run") and \
            os.path.isdir(f"/run/user/{os.getuid()}/systemd"):
        return " ".join(HEAVY) + " "
    return "nice -n 19 " if os.name == "posix" else ""


def status_line(st, now):
    last = st.get("audited_at")
    return ("stops audit: " + (f"last {(now - last) / DAY:.0f} d ago" if last else "never")
            + (f"; REQUESTED {(now - st['requested_at']) / DAY:.0f} d ago, not marked" if st.get("requested_at") else ""))


def main(argv):
    ap = argparse.ArgumentParser(description="stops audit of the hub's agents: schedule and guarantee")
    ap.add_argument("--check", action="store_true", help="is an audit due (the default)")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--every", type=int, default=7)
    ap.add_argument("--gap", type=int, default=2)
    ap.add_argument("--grow", type=int, default=10000, help="bytes of new lines after which the audit comes early")
    ap.add_argument("--min-cards", type=int, default=5)
    ap.add_argument("--share", type=float, default=0.8)
    ap.add_argument("--budget", type=int, default=600, help="seconds to read new journal archives in one --bundle")
    ap.add_argument("--done", nargs="*", metavar="TEXT")
    ap.add_argument("--skip", nargs="*", default=[], help="with --done: agents not checked in this audit")
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--only", nargs="*", default=[], help="with --extract: only these agents")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--bundle", action="store_true")
    a = ap.parse_args(argv)
    now = time.time()
    try:
        st = load_json(state_path(), {})
        if a.status:
            print(status_line(st, now))
            return 0
        if a.extract:
            n, cost, failed, known, other = extract(a.model, a.only)
            print(f"stops-audit: stops extracted {n}, price ${cost:.3f}; dropped as known: {known}, not waiting for the owner: {other}; "
                  f"{os.path.join(own_dir('bundle'), 'stops.md')}")
            if failed:   # a model failure is not "0 stops": no exit 0, --done will not mark these agents
                print(f"stops-audit: MODEL FAILURE, agents not parsed ({len(failed)}): "
                      + "; ".join(f"{k}: {v[0]}" for k, v in sorted(failed.items()))[:400]
                      + f"\nRepeat {command('--extract')} — it takes only them; the result above is incomplete", file=sys.stderr)
                return 1
            return 0
        agents = load_agents()
        if a.done is not None:
            skip = set(a.skip)   # not checked: the old snapshot stays, their delta goes into the next audit
            bad = sorted(set(st.get("extract_failed") or {}) - skip)
            if bad:   # the model did not read their delta: marking it audited would lose it
                print(f"stops-audit: --done rejected: --extract did not parse {', '.join(bad)} — repeat {command('--extract')} "
                      f"or mark them --skip (their delta goes into the next audit)", file=sys.stderr)
                return 1
            files = dict(st.get("files") or {})
            index = load_json(os.path.join(own_dir("bundle"), "index.json"), {})
            marked = []
            for n in agents:
                src = os.path.join(own_dir("pending"), n + ".md")
                # only the file as it went into the bundle: an agent the bundle did not have (new since, or no bundle
                # at all) keeps no snapshot — the next audit reads it whole
                if n in skip or n not in index or not os.path.exists(src):
                    continue
                shutil.copyfile(src, os.path.join(own_dir("snap"), n + ".md"))
                files[n] = index[n]["sha"]
                marked.append(n)
            write_text(state_path(), json.dumps({"audited_at": now, "files": files, "skipped": sorted(skip)},
                                                ensure_ascii=False, indent=1))
            # the bundle is used up: a later --done without a fresh --bundle remembers nothing
            for f in [os.path.join(own_dir("bundle"), "index.json"), *glob.glob(os.path.join(own_dir("pending"), "*.md"))]:
                if os.path.exists(f):
                    os.remove(f)
            with open(os.path.join(own_dir(), "history.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now, "agents": len(marked), "skipped": sorted(skip), "note": " ".join(a.done)},
                                   ensure_ascii=False) + "\n")
            print(f"stops-audit: audit marked (agents {len(marked)}"
                  + (f", skipped until the next one: {', '.join(sorted(skip))}" if skip else "") + ")")
            return 0
        if a.bundle:
            cards, left = load_cards(a.days, a.budget, now)
            d = write_bundle(agents, cards, st, a)
            if st.pop("extract_failed", None):   # the failures are of the old bundle: it is gone, the new one is read whole
                write_text(state_path(), json.dumps(st, ensure_ascii=False, indent=1))
            print(f"stops-audit: data {os.path.join(d, 'INDEX.md')} (agents {len(agents)}, cards over {a.days} d: "
                  f"{len(cards)}" + (f", journal archives not read to the end: {left} — run --bundle again" if left else "")
                  + f")\nNext: {command('--extract')} — stops by a cheap model, then section 6a of the agent-developer skill")
            return 0
        why = due(agents, st, a, now)
        if not why:
            return 0
        if not st.get("requested_at"):
            st["requested_at"] = now
            write_text(state_path(), json.dumps(st, ensure_ascii=False, indent=1))
    except Exception as e:   # noqa: BLE001 — the core delivers one line of a check error to the session
        print(f"stops-audit: error: {type(e).__name__}: {e}".splitlines()[0][:300])
        return 1
    done = command("--done", '"<result>"')
    print(f"stops-audit: {'; '.join(why)}.\nAgents with a file: {len(agents)}. The agent-developer skill, section 6a "
          f"(stops audit):\n1. data (reads the journal over {a.days} d; the first time — minutes): {heavy_prefix()}{command('--bundle')}\n"
          f"2. stops by a cheap model: {command('--extract')}\n"
          f"3. when done (and when there is nothing to bring): {done} [--skip <agent…>]")
    return NEW


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
