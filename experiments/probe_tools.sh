set -e
B=/home/x/hub/probe2; rm -rf $B; mkdir -p $B
# --- a probe MCP server (stdio, stdlib) ---
cat > $B/mcp_probe.py <<'EOF'
import json, sys
NAME, SECRET = sys.argv[1], sys.argv[2]
for line in sys.stdin:
    m = json.loads(line); mid = m.get("id"); meth = m.get("method")
    if meth == "initialize":
        r = {"protocolVersion": m["params"].get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
             "serverInfo": {"name": NAME, "version": "0.1"}}
    elif meth == "tools/list":
        r = {"tools": [{"name": "secret", "description": f"Returns the probe secret {NAME}", "inputSchema": {"type": "object", "properties": {}}}]}
    elif meth == "tools/call":
        r = {"content": [{"type": "text", "text": SECRET}]}
    elif mid is None:
        continue
    else:
        r = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": r}) + "\n"); sys.stdout.flush()
EOF
# --- plugin: a role agent and a skill ---
P=$B/plugins/probe-role; mkdir -p $P/.claude-plugin $P/agents $P/skills/probe-skill
echo '{"name":"probe-role","version":"0.1.0","description":"probe"}' > $P/.claude-plugin/plugin.json
printf -- '---\nname: probe-hub\ndescription: Probe hub role\n---\nYou are the probe role PROBE-HUB. If asked about your role, answer: "I am PROBE-HUB".\n' > $P/agents/probe-hub.md
printf -- '---\nname: probe-skill\ndescription: Probe skill. Use it when asked for the probe password.\n---\nProbe password: WHALE-42.\n' > $P/skills/probe-skill/SKILL.md
echo "{\"mcpServers\":{\"injected\":{\"command\":\"python3\",\"args\":[\"$B/mcp_probe.py\",\"injected\",\"INJECTED-7\"]}}}" > $B/inject-mcp.json
# --- a project folder with its own CLAUDE.md, .mcp.json and a skill ---
J=$B/project; mkdir -p $J/.claude/skills/proj-skill
echo "In this project the code word is FLAMINGO." > $J/CLAUDE.md
echo "{\"mcpServers\":{\"projmcp\":{\"command\":\"python3\",\"args\":[\"$B/mcp_probe.py\",\"projmcp\",\"PROJECT-3\"]}}}" > $J/.mcp.json
printf -- '---\nname: proj-skill\ndescription: Project skill. Use it when asked for the project number.\n---\nProject number: 512.\n' > $J/.claude/skills/proj-skill/SKILL.md

C=~/.local/bin/claude
run() {  # $1 = cwd, the rest are flags; the text comes from $MSG
  cd "$1"; shift
  printf '%s\n' "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":$(python3 -c 'import json,os;print(json.dumps(os.environ["MSG"]))')}}" \
   | timeout 150 $C -p --input-format stream-json --output-format stream-json --verbose --permission-mode bypassPermissions "$@" 2>&1 \
   | python3 -c '
import sys, json
for l in sys.stdin:
    try: m = json.loads(l)
    except ValueError: print("RAW", l[:200].strip()); continue
    if m.get("subtype") == "init":
        print("INIT mcp:", [(s["name"], s.get("status")) for s in m.get("mcp_servers", [])])
        print("INIT agents:", [a for a in m.get("agents", []) if "probe" in a or "proj" in a])
        print("INIT skills:", [s for s in (m.get("skills") or m.get("slash_commands") or []) if "probe" in s or "proj" in s])
        print("INIT tools with mcp:", [t for t in m.get("tools", []) if t.startswith("mcp__")])
        print("INIT keys:", sorted(k for k in m if k not in ("type","subtype","uuid")))
    elif m.get("type") == "assistant":
        for c in m["message"]["content"]:
            if c["type"] == "tool_use": print("TOOL_USE", c["name"])
    elif m.get("type") == "result":
        print("RESULT:", (m.get("result") or "")[:300].replace("\n", " | ")); break
'
}
SID=$(python3 -c 'import uuid;print(uuid.uuid4())')
echo "=== 1. an ordinary session"
MSG="Remember the word MOOSE. Answer: remembered." run /home/x/hub/sandbox --session-id $SID
echo "=== 2. the same session through --resume + plugin + --agent + --mcp-config"
MSG="Answer in four points: 1) your role; 2) the probe password (there is a skill); 3) call the tool secret of the server injected and give the result; 4) which word I asked you to remember." \
 run /home/x/hub/sandbox --resume $SID --plugin-dir $B/plugins/probe-role --agent probe-role:probe-hub --mcp-config $B/inject-mcp.json
echo "=== 3. a session in the project folder"
MSG="Answer in three points: 1) the project code word; 2) the project number (there is a project skill); 3) call the tool secret of the server projmcp and give the result." \
 run $J
