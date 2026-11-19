"""A stand-in MCP server with an OAuth 2.1 sign-in (streamable HTTP, JSON answers) for tests of the sign-in from the page
with a real claude. /mcp without a token answers 401 with WWW-Authenticate (resource_metadata), as real ones do; the
discovery documents, dynamic client registration and PKCE S256 are there. /authorize shows nothing: it answers 302 to
redirect_uri with code and state — the "owner's browser" in a test is an HTTP client that reads Location. Tokens live in
memory. seen — what the server was asked (without codes and tokens)."""
import base64
import hashlib
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse


class OAuthMcp:
    def __init__(self):
        codes, tokens, seen, lock = {}, set(), [], threading.Lock()
        self.seen = seen

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def send(self, code, body=None, headers=None):
                data = b"" if body is None else json.dumps(body).encode()
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if data:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u, base = urlparse(self.path), f"http://127.0.0.1:{self.server.server_address[1]}"
                q = parse_qs(u.query)
                with lock:
                    seen.append(("GET", u.path))
                if u.path.startswith("/.well-known/oauth-protected-resource"):
                    return self.send(200, {"resource": base + "/mcp", "authorization_servers": [base]})
                if u.path.startswith(("/.well-known/oauth-authorization-server", "/.well-known/openid-configuration")):
                    return self.send(200, {
                        "issuer": base, "authorization_endpoint": base + "/authorize", "token_endpoint": base + "/token",
                        "registration_endpoint": base + "/register", "response_types_supported": ["code"],
                        "grant_types_supported": ["authorization_code", "refresh_token"],
                        "code_challenge_methods_supported": ["S256"], "token_endpoint_auth_methods_supported": ["none"]})
                if u.path == "/authorize":
                    code = secrets.token_urlsafe(16)
                    back = q.get("redirect_uri", [""])[0]
                    with lock:
                        codes[code] = q.get("code_challenge", [""])[0]
                    where = back + ("&" if "?" in back else "?") + urlencode({"code": code, "state": q.get("state", [""])[0]})
                    return self.send(302, headers={"Location": where})
                self.send(405 if u.path == "/mcp" else 404)

            def do_DELETE(self):
                self.send(405)

            def do_POST(self):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                with lock:
                    seen.append(("POST", u.path))
                if u.path == "/register":
                    d = json.loads(raw or b"{}")
                    return self.send(201, {"client_id": "c-" + secrets.token_hex(4), "redirect_uris": d.get("redirect_uris", []),
                                           "token_endpoint_auth_method": "none", "response_types": ["code"],
                                           "grant_types": ["authorization_code", "refresh_token"]})
                if u.path == "/token":
                    f = {k: v[0] for k, v in parse_qs(raw.decode()).items()}
                    with lock:
                        challenge = codes.pop(f.get("code", ""), None)
                    verifier = f.get("code_verifier", "")
                    ok = f.get("grant_type") == "refresh_token" or (challenge is not None and (not challenge or challenge == (
                        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode())))
                    if not ok:
                        return self.send(400, {"error": "invalid_grant"})
                    token = "at-" + secrets.token_hex(8)
                    with lock:
                        tokens.add(token)
                    return self.send(200, {"access_token": token, "token_type": "Bearer", "expires_in": 3600,
                                           "refresh_token": f.get("refresh_token") or "rt-" + secrets.token_hex(8)})
                if u.path == "/mcp":
                    auth = self.headers.get("Authorization", "")
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        return self.send(400)
                    with lock:
                        known = auth.startswith("Bearer ") and auth[7:] in tokens
                    if not known:
                        base = f"http://127.0.0.1:{self.server.server_address[1]}"
                        return self.send(401, {"error": "unauthorized"}, {
                            "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'})
                    if "id" not in msg:
                        return self.send(202)
                    method = msg.get("method")
                    if method == "initialize":
                        res = {"protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-03-26"),
                               "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-oauth", "version": "0"}}
                    elif method == "tools/list":
                        res = {"tools": [{"name": "ping", "description": "pong",
                                          "inputSchema": {"type": "object", "properties": {}}}]}
                    else:
                        res = {}
                    return self.send(200, {"jsonrpc": "2.0", "id": msg["id"], "result": res}, {"Mcp-Session-Id": "s1"})
                self.send(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/mcp"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
