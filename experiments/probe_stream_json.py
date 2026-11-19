"""A probe of claude -p stream-json: authorization, several turns, hooks, AskUserQuestion, permissions."""
import json, os, subprocess, sys, threading, time, uuid, queue

CLAUDE = os.path.expanduser("~/.local/bin/claude")
STATE = "/home/x/hub/probe"; os.makedirs(STATE + "/spool", exist_ok=True)
sid = str(uuid.uuid4())
mode = sys.argv[1] if len(sys.argv) > 1 else "basic"
args = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--session-id", sid, "--settings", "/home/x/hub/state/hooks-settings.json"]
if mode == "perm":
    args += ["--permission-mode", "default", "--permission-prompt-tool", "stdio"]
env = dict(os.environ, HUB_SESSION="probe-" + mode, HUB_STATE_DIR=STATE)
p = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                     env=env, cwd="/home/x/hub/sandbox", bufsize=1)
q = queue.Queue()
threading.Thread(target=lambda: [q.put(l) for l in p.stdout], daemon=True).start()
threading.Thread(target=lambda: [q.put("STDERR " + l) for l in p.stderr], daemon=True).start()

def send(obj):
    p.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n"); p.stdin.flush()

def user(text):
    send({"type": "user", "message": {"role": "user", "content": text}})

def read_until(pred, timeout=90):
    end = time.time() + timeout
    while time.time() < end:
        try: line = q.get(timeout=1)
        except queue.Empty: continue
        if line.startswith("STDERR"):
            print(line.strip()[:300]); continue
        try: m = json.loads(line)
        except ValueError: print("RAW", line[:200]); continue
        t = m.get("type"); st = m.get("subtype")
        if t == "system" and st == "init":
            print("INIT apiKeySource=", m.get("apiKeySource"), "model=", m.get("model"), "permissionMode=", m.get("permissionMode"),
                  "tools has AskUserQuestion:", "AskUserQuestion" in (m.get("tools") or []))
        elif t == "assistant":
            for c in m["message"]["content"]:
                if c["type"] == "text": print("ASSISTANT:", c["text"][:200])
                elif c["type"] == "tool_use": print("TOOL_USE:", c["name"], json.dumps(c["input"], ensure_ascii=False)[:300])
        elif t == "user":
            for c in (m["message"]["content"] if isinstance(m["message"]["content"], list) else []):
                if c.get("type") == "tool_result": print("TOOL_RESULT:", str(c.get("content"))[:300])
        elif t == "result":
            print("RESULT", st, "is_error=", m.get("is_error"), "cost=", m.get("total_cost_usd"), "turns=", m.get("num_turns"))
        elif t == "control_request":
            print("CONTROL_REQUEST", json.dumps(m, ensure_ascii=False)[:500])
        else:
            print("MSG", t, st)
        if pred(m): return m
    print("TIMEOUT"); return None

is_result = lambda m: m.get("type") == "result"
if mode == "basic":
    user("Answer with one word: one"); read_until(is_result)
    user("Which word did you answer last time? One word."); read_until(is_result)
    # a message in the middle of a turn
    user("Count slowly from 1 to 30 in words, each number as a word, separated by commas.")
    time.sleep(2); user("Stop. Answer only: second received")
    read_until(is_result); read_until(is_result, 60)
elif mode == "ask":
    user("Call the tool AskUserQuestion: the question \"Which option?\", options A and B. Then say which was chosen.")
    read_until(is_result)
elif mode == "perm":
    ctl = lambda m: m.get("type") == "control_request" or is_result(m)
    user("Create the file /home/x/hub/sandbox/probe.txt with the text hi through the tool Write. Then call AskUserQuestion: \"Which option?\" A/B, and say which was chosen.")
    while True:
        m = read_until(ctl)
        if not m or is_result(m): break
        req = m["request"]
        if req.get("subtype") == "can_use_tool":
            inp = req.get("input", {})
            if req.get("tool_name") == "AskUserQuestion":
                qs = inp.get("questions", [])
                inp = dict(inp, answers={qs[0]["question"]: "B"} if qs else {})
            send({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"],
                  "response": {"behavior": "allow", "updatedInput": inp}}})
p.stdin.close()
try: p.wait(20)
except subprocess.TimeoutExpired: p.kill()
print("EXIT", p.returncode, "SID", sid)
print("HOOK EVENTS:", sorted(json.load(open(os.path.join(STATE, "spool", f)))["type"] for f in os.listdir(STATE + "/spool") if f.endswith(".json")))
for f in os.listdir(STATE + "/spool"): os.unlink(os.path.join(STATE, "spool", f))
